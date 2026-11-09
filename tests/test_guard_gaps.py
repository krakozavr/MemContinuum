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
import threading
import time
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

        def spy(blob, label, **kw):
            seen.append(label)
            return real(blob, label, **kw)

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

    def test_staged_bad_types_beside_a_ruling_edit_exit_1_and_report_the_edit(self):
        """Grok round 1: the crash used to hide the summary, so the pre-commit hook read the
        run as an engine failure and let the commit through."""
        root = self.store(ttext([]))
        for name, bad in (("list.md", "type: [topic, weird]\n"), ("map.md", "type: {a: b}\n")):
            (root / "topics" / name).write_text(
                ttext([], tid="TOP-9105" if name == "list.md" else "TOP-9106").replace("type: topic\n", bad, 1))
        foo = root / "topics" / "foo.md"
        foo.write_text(foo.read_text().replace('"fourth"', '"fourth, edited"', 1))
        git(["add", "-A"], root)
        (root / "topics" / "list.md").unlink()
        (root / "topics" / "map.md").unlink()
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(root)])
        self.assertEqual(rc, 1, out)
        self.assertNotIn("unexpected failure", out)
        self.assertIn("type must be a scalar string (added file)", out)
        self.assertRegex(out, r"changed=[1-9]")

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

    # R2-1 / R2-2: the REF side is read ONCE, as the index delivered it (parse_record_text
    # `as_delivered`), for every check the guard makes -- a repair never releases history.
    @staticmethod
    def _incident(refs=("src/a.py",), meta=True, extra="", typ="type: incident\n"):
        body = "code_refs:\n" + "".join(f"  - \"{r}\"\n" for r in refs) if refs else "code_refs: []\n"
        return ("---\n" + typ + "id: INC-9150\ntitle: Odd\n" + body
                + ("metadata:\n  type: [x]\n" if meta else "") + extra + "---\n\nBody.\n")

    def test_a_ref_incident_with_bad_metadata_type_is_valid_as_delivered(self):
        res = memidx.parse_record_text(self._incident(), "x", as_delivered=True)
        self.assertTrue(res.valid, res)
        self.assertEqual(res.frontmatter["metadata"], {"type": ["x"]})
        self.assertFalse(memidx.parse_record_text(self._incident(), "x").valid)

    def test_repairing_the_metadata_alone_on_an_incident_is_clean(self):
        self.store(self._incident())
        self.assertClean(self._incident(meta=False))

    def test_repair_plus_dropping_a_reference_without_a_row_on_an_incident_is_refused(self):
        self.store(self._incident())
        self.assertRefused(self._incident(refs=(), meta=False),
                           "code_refs changed for src/a.py (remove) with no matching ref_marks row")
        self.assertClean(self._incident(refs=(), meta=False, extra="ref_marks:\n" + rmark("src/a.py", "remove")))

    def test_repairing_the_metadata_of_a_topic_alone_is_clean(self):
        self.store(self._bad_metadata(ttext(["src/a.py"])))
        self.assertClean(ttext(["src/a.py"]))

    def test_repair_plus_dropping_a_reference_without_a_row_on_a_topic_is_refused(self):
        self.store(self._bad_metadata(ttext(["src/a.py"])))
        self.assertRefused(ttext([]), "code_refs changed for src/a.py (remove) with no matching ref_marks row")

    def test_repair_plus_rewriting_a_recorded_ruling_on_a_topic_is_refused(self):
        self.store(self._bad_metadata(ttext(["src/a.py"])))
        self.assertRefused(ttext(["src/a.py"]).replace('"fourth"', '"fourth, edited"', 1),
                           "link field changed after being recorded")

    # round 2 bypass: the REF's links chain was read from the repaired parse, so on an incident the
    # ruling edit rode in with the metadata repair.
    def _incident_with_links(self, meta=True, text="first"):
        return self._incident(meta=meta, extra="links:\n" + link_yaml("L1", text=text))

    def test_repairing_the_metadata_of_an_incident_with_links_alone_is_clean(self):
        self.store(self._incident_with_links())
        self.assertClean(self._incident_with_links(meta=False))

    def test_repair_plus_rewriting_a_recorded_ruling_on_an_incident_is_refused(self):
        self.store(self._incident_with_links())
        self.assertRefused(self._incident_with_links(meta=False, text="first, edited"),
                           "link field changed after being recorded")

    def test_repair_plus_dropping_an_anchor_row_on_a_topic_is_refused(self):
        refs = ("src/foo.py",)
        old = binding_topic([good_row()], code_refs=refs).replace("tags: []\n", "tags: []\nmetadata:\n  type: [x]\n", 1)
        self.assertTrue(memidx.parse_record_text(old, "x", as_delivered=True).valid)
        self.store(old)
        self.assertClean(binding_topic([good_row()], code_refs=refs))
        rc, out = self.guard(binding_topic(None, code_refs=refs))
        self.assertEqual(rc, 1, out)

    def test_a_list_type_at_ref_repaired_to_topic_with_no_other_change_is_clean(self):
        # the one carve-out: a truthy non-string `type` delivered a repr string nobody can keep
        self.store(self._listy())
        self.assertClean(ttext([]))

    # R2-2: a NOTE's malformed type is dropped for CURRENT indexing only; at REF the old index
    # delivered `str(type)`, a non-canonical string, so nothing is frozen.
    def _note42(self):
        return "---\ntype: 42\ntitle: A note\n---\n\nBody.\n"

    def test_a_note_with_an_int_type_at_ref_froze_nothing_so_it_may_become_a_memo_or_be_deleted(self):
        root = self.store(self._note42())
        self.assertClean("---\ntype: memo\ntitle: A note\n---\n\nBody.\n")
        (root / "topics" / "foo.md").unlink()
        rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
        self.assertEqual(rc, 0, out)

    def test_a_note_with_an_int_type_is_still_indexed_without_it_and_warned_about_now(self):
        res = memidx.parse_record_text(self._note42(), "topics/x.md")
        self.assertNotIn("type", res.frontmatter)
        self.assertIn(memidx.TYPE_SHAPE_DIAGNOSTIC[0], [d[0] for d in res.diagnostics])
        self.assertEqual(memidx.parse_record_text(self._note42(), "x", as_delivered=True).frontmatter["type"], 42)

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
        # gate round 1 R1-6: commit the removal first. An unbind and a bind of the same entry with no
        # commit between them leave a `remove` and an `add` row for a membership that did not change
        # against HEAD, which the guard refuses as orphan rows.
        commit_all(self.store)
        rc, out, err = self.bind("src/*.py")  # a glob is bound again, without an anchor (TOP-0137 L29)
        self.assertEqual(rc, 0, err)
        self.assertIn("bound without an anchor", out)
        self.assertFalse(self.fm().get("anchors"))
        self.assertEqual([(r["ref"], r["action"]) for r in memidx.ref_mark_rows(self.fm())][0],
                         ("src/*.py", "add"))
        self.assertGuardsClean()

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


class TestUndoReceipts(BindCase):
    """TOP-0137 L34: `bind`/`unbind` record an exact undo receipt; the opposite
    command restores the pre-command bytes while the topic, HEAD and the staged
    entry are what the receipt recorded, and refuses (writing nothing) otherwise."""

    def rec_dir(self, store=None):
        out = git(["rev-parse", "--path-format=absolute", "--git-path", "memcontinuum/receipts"],
                  store or self.store).stdout.strip()
        return Path(out)

    def receipts(self):
        d = self.rec_dir()
        return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []

    def raw(self):
        return self.path.read_bytes()

    def head_state(self, refs=("src/other.py",)):
        """A committed topic with `refs` bound (the guard-clean starting fixture)."""
        self.put(binding_topic(code_refs=refs))
        return self.raw()

    def bound_head(self):
        self.put(binding_topic(code_refs=("src/other.py",)))
        self.assertEqual(self.bind()[0], 0)
        commit_all(self.store)
        return self.raw()

    def assertUndone(self, result, before, clean=True):
        rc, out, err = result
        self.assertEqual(rc, 0, err)
        self.assertIn("undo: restored TOP-1 to its state before the uncommitted", out)
        self.assertEqual(self.raw(), before)
        if clean:
            self.assertGuardsClean()

    def assertRefused(self, result, snippet):
        rc, out, err = result
        self.assertEqual(rc, 1, (out, err))
        self.assertIn(snippet, err)
        self.assertIn("nothing written", err)

    def test_bind_then_unbind_restores_the_exact_bytes(self):
        before = self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        self.assertNotEqual(self.raw(), before)
        self.assertEqual(len(self.receipts()), 1)
        self.assertUndone(self.unbind(), before)
        self.assertEqual(self.receipts(), [])
        self.assertNotIn("anchors:", self.text())

    def test_unbind_then_bind_restores_the_exact_bytes(self):
        before = self.bound_head()
        self.assertEqual(self.unbind()[0], 0)
        self.assertUndone(self.bind(), before)
        self.assertEqual(self.receipts(), [])

    def test_repeated_cycles_return_to_the_start_each_time(self):
        before = self.head_state(refs=())
        for _ in range(3):
            self.assertEqual(self.bind()[0], 0)
            self.assertUndone(self.unbind(), before)
            self.assertEqual(self.receipts(), [])
        before = self.bound_head()
        for _ in range(3):
            self.assertEqual(self.unbind()[0], 0)
            self.assertUndone(self.bind(), before)
            self.assertEqual(self.receipts(), [])

    def test_the_same_second_makes_no_difference(self):
        before = self.head_state(refs=())
        fixed = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        with mock.patch.object(memidx, "_now", return_value=fixed):
            self.assertEqual(self.bind()[0], 0)
            self.assertUndone(self.unbind(), before)

    def test_a_new_topic_in_a_store_with_a_valid_head(self):
        (self.store / "notes.txt").write_text("x\n")
        commit_all(self.store)
        self.path.write_text(binding_topic(code_refs=()))  # never committed
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        self.assertUndone(self.unbind(), before)
        self.assertEqual(self.bind("src/*.py")[0], 0)
        self.assertUndone(self.unbind("src/*.py"), before)

    def test_quoted_and_crlf_ids_still_undo_to_the_exact_bytes(self):
        for name, mutate in (
                ("double-quoted", lambda t: t.replace("id: TOP-1", 'id: "TOP-1"')),
                ("single-quoted", lambda t: t.replace("id: TOP-1", "id: 'TOP-1'  # note")),
                ("crlf", lambda t: t.replace("\n", "\r\n")),
        ):
            with self.subTest(name):
                self.path.write_bytes(mutate(binding_topic(code_refs=())).encode())
                commit_all(self.store)
                before = self.raw()
                self.assertEqual(self.bind()[0], 0)
                self.assertUndone(self.unbind(), before)
                self.assertEqual(self.receipts(), [])

    def test_a_renamed_topic_is_refused_not_treated_as_new(self):
        self.bound_head()
        self.assertEqual(self.unbind()[0], 0)
        git(["mv", "topics/foo.md", "topics/bar.md"], self.store)
        moved = self.store / "topics" / "bar.md"
        before = moved.read_bytes()
        self.assertRefused(self.bind(), "the topic moved from topics/foo.md to topics/bar.md")
        self.assertEqual(moved.read_bytes(), before)

    def test_a_restored_anchor_is_in_b_and_survives(self):
        # HEAD: foo unbound with its frozen anchor; bind RESTORES it (captures nothing)
        self.bound_head()
        self.assertEqual(self.unbind()[0], 0)
        commit_all(self.store)
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        self.assertEqual(len(memidx.anchor_rows(self.fm())), 1)
        self.assertUndone(self.unbind(), before)
        self.assertEqual(len(memidx.anchor_rows(self.fm())), 1)

    def test_an_unrelated_pending_anchor_survives_a_nested_undo(self):
        before = self.head_state(refs=())
        self.assertEqual(self.bind("src/other.py")[0], 0)  # pending, uncommitted
        mid = self.raw()
        self.assertEqual(self.bind("src/foo.py")[0], 0)
        self.assertUndone(self.unbind("src/foo.py"), mid)
        self.assertEqual([r["ref"] for r in memidx.anchor_rows(self.fm())], ["src/other.py"])
        self.assertUndone(self.unbind("src/other.py"), before)
        self.assertEqual(self.receipts(), [])

    L2_LINK = (
        "  - link: L2\n    date: '2024-02-01'\n    status: active\n    kind: adopted\n"
        "    ruling:\n      text: \"second ruling\"\n      authority: agent-inference\n")

    def two_link_topic(self, rows=None):
        """HEAD: links L1/L2, current L2, a committed `remove` for src/foo.py, no anchors."""
        text = binding_topic(rows, code_refs=("src/other.py",), extra_links=self.L2_LINK,
                             marks_text=ref_mark_text("src/foo.py", "remove"))
        return text.replace("current: L1\n", "current: L2\n", 1)

    def test_a_pending_anchor_for_the_same_key_that_bind_restores_is_in_b(self):
        # hygiene-3 round 1: B already holds a valid uncommitted anchor for the key bind restores
        self.put(self.two_link_topic())
        self.path.write_text(self.two_link_topic([good_row(link="L2")]))
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        self.assertEqual([a["link"] for a in memidx.anchor_rows(self.fm())], ["L2"])  # restored, none added
        self.assertUndone(self.unbind(), before)
        self.assertEqual([a["link"] for a in memidx.anchor_rows(self.fm())], ["L2"])

    def test_an_unrelated_pending_anchor_for_the_same_reference_at_another_link_survives(self):
        # hygiene-3 round 2: the pending L1 anchor is in B; bind captures the L2 one; undo keeps L1
        self.put(self.two_link_topic())
        self.path.write_text(self.two_link_topic([good_row(link="L1")]))
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        self.assertEqual(sorted(a["link"] for a in memidx.anchor_rows(self.fm())), ["L1", "L2"])
        self.assertUndone(self.unbind(), before)
        self.assertEqual([a["link"] for a in memidx.anchor_rows(self.fm())], ["L1"])

    def test_a_multi_line_quoted_value_containing_a_hash_is_in_b(self):
        # hygiene-3 round 2: the quoted reference spans two physical lines and carries `# drafts`
        self.put(binding_topic(code_refs=('"docs/*\n    # drafts"', "src/other.py")))
        self.assertEqual(self.fm()["code_refs"], ["docs/* # drafts", "src/other.py"])
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        self.assertUndone(self.unbind(), before)
        self.assertEqual(self.fm()["code_refs"], ["docs/* # drafts", "src/other.py"])

    def test_a_committed_anchor_with_an_edited_field_survives(self):
        self.bound_head()
        edited = self.text().replace('worktree: clean', 'worktree: dirty', 1)
        self.assertNotEqual(edited, self.text())
        self.path.write_text(edited)
        before = self.raw()
        self.assertEqual(self.unbind()[0], 0)
        self.assertUndone(self.bind(), before, clean=False)
        self.assertIn("worktree: dirty", self.text())

    def test_a_trailing_comment_is_in_b(self):
        self.put(binding_topic(code_refs=()).replace("---\n\nBody.", "# a trailing comment\n---\n\nBody.", 1))
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        self.assertUndone(self.unbind(), before)
        self.assertIn(b"# a trailing comment\n", self.raw())

    def test_a_ruling_appended_between_is_refused_with_the_recovery_options(self):
        before = self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        after = self.raw()
        text = self.text().replace("current: L1", "current: L2").replace(
            "links:\n", "links:\n" + link_yaml("L2", text="a newer ruling"), 1)
        self.path.write_text(text)
        changed = self.raw()
        rc, out, err = self.unbind()
        self.assertEqual(rc, 1, err)
        self.assertIn("the topic changed after the uncommitted bind of src/foo.py", err)
        self.assertIn("commit first, then unbind", err)
        self.assertIn("git restore --source=HEAD --worktree -- topics/foo.md", err)
        self.assertIn("add row for src/foo.py", err)  # the pending row is named
        self.assertEqual(self.raw(), changed)
        self.assertNotEqual(changed, after)
        # the explicit command refuses the same way
        rc, out, err = run_memidx(["undo-binding", "TOP-1", "--root", str(self.store)])
        self.assertEqual(rc, 1, err)
        self.assertEqual(self.raw(), changed)
        self.assertNotEqual(changed, before)

    def test_a_damaged_topic_between_bind_and_unbind_still_gets_the_recovery_message(self):
        # gate round 1 (Sol 5): the refusal needs no YAML, so the parse error must not hide it
        self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        damaged = self.text().replace("code_refs:", "code_refs: [unclosed", 1).encode()
        self.assertNotEqual(damaged, self.raw())
        self.path.write_bytes(damaged)
        for argv in (["unbind", "TOP-1", "src/foo.py"], ["bind", "TOP-1", "src/foo.py", "--code-root", str(self.code)],
                     ["undo-binding", "TOP-1"]):
            with self.subTest(argv[0]):
                rc, out, err = run_memidx(argv + ["--root", str(self.store)])
                self.assertEqual(rc, 1, (out, err))
                if argv[0] == "bind":  # not the opposite command: the ordinary path, which needs a parse
                    self.assertIn("does not parse cleanly", err)
                    continue
                self.assertIn("the topic changed after the uncommitted bind of src/foo.py", err)
                self.assertIn("git restore --source=HEAD --worktree -- topics/foo.md", err)
                self.assertIn("add row for src/foo.py", err)
                self.assertEqual(self.raw(), damaged)
        self.assertEqual(len(self.receipts()), 1)

    def test_a_commit_between_drops_the_receipt_and_the_ordinary_unbind_proceeds(self):
        self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        commit_all(self.store)
        rc, out, err = self.unbind()
        self.assertEqual(rc, 0, err)
        self.assertIn("is now committed history", out)
        self.assertNotIn("undo:", out)
        self.assertEqual([(r["ref"], r["action"]) for r in memidx.ref_mark_rows(self.fm())],
                         [("src/foo.py", "remove"), ("src/foo.py", "add")])
        self.assertGuardsClean()
        self.assertEqual(self.receipts(), [self.receipts()[0]])  # only the new unbind's receipt
        self.assertEqual(len(self.receipts()), 1)

    def test_an_unrelated_commit_between_bind_and_unbind_does_not_expire_the_receipt(self):
        # gate round 1 (Grok 1): HEAD moving is not the topic's committed content changing
        before = self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        (self.store / "notes.txt").write_text("unrelated\n")
        git(["add", "notes.txt"], self.store)
        git(["commit", "-q", "-m", "unrelated"], self.store)
        self.assertUndone(self.unbind(), before)
        self.assertEqual(self.receipts(), [])

    def test_a_topic_committed_with_the_binding_is_committed_history(self):
        self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        commit_all(self.store)
        rc, out, err = self.unbind()
        self.assertEqual(rc, 0, err)
        self.assertIn("is now committed history (the committed topics/foo.md holds it)", out)
        self.assertNotIn("undo:", out)
        self.assertGuardsClean()

    def test_a_partial_commit_of_an_earlier_edit_does_not_make_the_binding_committed(self):
        # gate round 1 (Sol's counterexample): B carries an uncommitted ruling edit, bind makes A, and
        # only the ruling edit is committed. HEAD's topic blob changed but does not contain the binding.
        self.head_state(refs=())
        edited = self.text().replace("current: L1", "current: L2").replace(
            "links:\n", "links:\n" + link_yaml("L2", text="a newer ruling"), 1)
        self.path.write_text(edited)
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        after = self.raw()
        self.path.write_bytes(before)  # the version with the ruling and without the mark
        git(["add", "topics/foo.md"], self.store)
        git(["commit", "-q", "-m", "the ruling only"], self.store)
        self.path.write_bytes(after)
        rc, out, err = self.unbind()
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("the committed topics/foo.md changed after the uncommitted bind of src/foo.py "
                      "but does not contain it", err)
        self.assertIn("git restore --source=HEAD --worktree -- topics/foo.md", err)
        self.assertIn("nothing written", err)
        self.assertEqual(self.raw(), after)
        self.assertEqual(len(self.receipts()), 1)

    def test_an_unavailable_git_probe_refuses_without_dropping_or_writing(self):
        before = self.head_state(refs=())
        with mock.patch.object(memidx, "_head_state", return_value=None):
            rc, out, err = self.bind()  # no receipt can be recorded: refused, nothing written
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("git state unavailable for topics/foo.md (HEAD or its tree); nothing written", err)
        self.assertIn("retry, or commit first", err)
        self.assertEqual((self.raw(), self.receipts()), (before, []))
        self.assertEqual(self.bind()[0], 0)
        after = self.raw()
        for name in ("_head_state", "_staged_state"):
            with self.subTest(name), mock.patch.object(memidx, name, return_value=None):
                rc, out, err = self.unbind()
            self.assertEqual(rc, 1, (out, err))
            self.assertIn("git state unavailable for topics/foo.md", err)
            self.assertEqual((self.raw(), len(self.receipts())), (after, 1))
        with mock.patch.object(memidx, "_blob_oid", return_value=None):
            rc, out, err = self.unbind()
        self.assertEqual((rc, len(self.receipts())), (1, 1), err)
        self.assertIn("(hash-object)", err)
        self.assertUndone(self.unbind(), before)

    def test_head_probe_results_are_explicit_states(self):
        run = lambda rc, out="": (rc, out)  # noqa: E731
        script = {("rev-parse",): run(1), ("symbolic-ref",): run(0, "refs/heads/main\n"),
                  ("show-ref",): run(1)}
        with mock.patch.object(memidx, "_git_run", side_effect=lambda store, args, data=None: script[(args[0],)]):
            self.assertEqual(memidx._head_state(self.store), {"kind": "unborn"})
            script[("show-ref",)] = run(128)  # a lookup error is not an absence
            self.assertIsNone(memidx._head_state(self.store))
            script[("show-ref",)] = run(0)  # the branch exists though HEAD does not resolve
            self.assertIsNone(memidx._head_state(self.store))
            script[("symbolic-ref",)] = run(1)  # detached or unreadable
            self.assertIsNone(memidx._head_state(self.store))
        self.assertIsNone(memidx._head_state(self.tmp / "no-such-dir"))

    def test_an_unborn_head_undoes_until_the_topic_itself_is_committed(self):
        self.path.write_text(binding_topic(code_refs=()))  # no commit exists in this store
        before = self.raw()
        self.assertEqual(self.bind()[0], 0)
        rc, out, err = run_memidx(["undo-binding", "TOP-1", "--list", "--root", str(self.store)])
        self.assertEqual(rc, 0, err)
        self.assertIn("baseline=unborn", out)
        self.assertUndone(self.unbind(), before, clean=False)
        self.assertEqual(self.bind()[0], 0)
        (self.store / "notes.txt").write_text("the first commit\n")  # an unrelated first commit
        git(["add", "notes.txt"], self.store)
        git(["commit", "-q", "-m", "first"], self.store)
        self.assertUndone(self.unbind(), before, clean=False)
        self.assertEqual(self.bind()[0], 0)
        commit_all(self.store)  # now the topic itself is committed, with the binding
        rc, out, err = self.unbind()
        self.assertEqual(rc, 0, err)
        self.assertIn("is now committed history", out)
        self.assertNotIn("undo:", out)
        self.assertGuardsClean()

    def test_the_committed_history_note_names_only_the_receipt_that_would_have_been_undone(self):
        # gate round 1 (Sol 6)
        self.head_state(refs=())
        self.assertEqual(self.bind("src/foo.py")[0], 0)
        self.assertEqual(self.bind("src/other.py")[0], 0)
        commit_all(self.store)
        rc, out, err = self.unbind("src/foo.py")  # the latest receipt is for `other`: not eligible
        self.assertEqual(rc, 0, err)
        self.assertNotIn("committed history", out)
        # `other`'s receipt was committed history and went silently, with the one beneath it: the stack
        # holds uncommitted work only
        self.assertEqual(len(self.receipts()), 1)
        commit_all(self.store)
        # the stack is [unbind foo], committed now; the eligible receipt is named, once
        rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.count("committed history"), 1)
        self.assertIn("unbind of src/foo.py is now committed history", out)
        self.assertEqual(len(self.receipts()), 1)  # only this bind's own receipt remains

    def test_staging_after_the_command_is_refused_and_unstaging_allows_the_undo(self):
        before = self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        git(["add", "topics/foo.md"], self.store)
        changed = self.raw()
        rc, out, err = self.unbind()
        self.assertEqual(rc, 1, err)
        self.assertIn("staged entry of topics/foo.md changed after the uncommitted bind", err)
        self.assertIn("git restore --staged -- topics/foo.md", err)
        self.assertIn("nothing was rewritten", err)
        self.assertEqual(self.raw(), changed)
        git(["restore", "--staged", "--", "topics/foo.md"], self.store)
        self.assertUndone(self.unbind(), before)

    def test_receipts_survive_a_process_restart(self):
        before = self.head_state(refs=())
        env = dict(os.environ, PYTHONPATH="", MEMCONTINUUM_TRANSCRIPTS_DIR=str(self.tmp / "tr"))
        base = [sys.executable, str(TOOLS_DIR / "memidx.py")]
        done = subprocess.run(base + ["bind", "TOP-1", "src/foo.py", "--root", str(self.store),
                                      "--code-root", str(self.code)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(self.raw(), before)
        done = subprocess.run(base + ["unbind", "TOP-1", "src/foo.py", "--root", str(self.store)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("undo: restored TOP-1", done.stdout)
        self.assertEqual(self.raw(), before)
        self.assertGuardsClean()

    def test_a_receipt_whose_topic_write_never_happened_is_dropped(self):
        before = self.head_state(refs=())
        real = memidx._atomic_write_bytes

        def crash_on_topic(path, data, exclusive=False, **kw):
            if Path(path).name == "foo.md":
                raise OSError("simulated crash after the receipt")
            return real(path, data, exclusive, **kw)

        with mock.patch.object(memidx, "_atomic_write_bytes", side_effect=crash_on_topic):
            with self.assertRaises(OSError):
                self.bind()
        self.assertEqual(self.raw(), before)
        self.assertEqual(len(self.receipts()), 1)  # the orphan receipt is on disk
        rc, out, err = self.unbind()  # the opposite command: orphan dropped, ref not bound
        self.assertEqual(rc, 0, err)
        self.assertIn("is not in", out)
        self.assertNotIn("undo:", out)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.raw(), before)
        # and an orphan under a retried bind is dropped before the fresh receipt is written
        with mock.patch.object(memidx, "_atomic_write_bytes", side_effect=crash_on_topic):
            with self.assertRaises(OSError):
                self.bind()
        self.assertEqual(self.bind()[0], 0)
        self.assertEqual(len(self.receipts()), 1)  # the orphan was pruned; only the fresh receipt remains
        self.assertUndone(self.unbind(), before)

    def test_competing_writers_the_loser_sees_the_changed_image(self):
        before = self.head_state(refs=())
        real = memidx.bound_edit_problem
        winner = {}

        def race(*a, **kw):
            if not winner:  # the competing bind runs to completion inside the loser's read-to-write window
                winner["rc"] = None
                winner["rc"] = self.bind("src/other.py")[0]
            return real(*a, **kw)

        with mock.patch.object(memidx, "bound_edit_problem", side_effect=race):
            rc, out, err = self.bind("src/foo.py")
        self.assertEqual(winner["rc"], 0)
        self.assertEqual(rc, 1, err)
        self.assertIn("the topic changed while this command ran; nothing written", err)
        self.assertEqual(self.fm()["code_refs"], ["src/other.py"])
        # the winner's receipt is intact and undoes exactly the winner
        self.assertUndone(self.unbind("src/other.py"), before)

    def test_a_concurrent_command_cannot_prune_an_in_flight_receipt(self):
        # gate round 1 (Sol 1, Grok 2): a second process between the receipt and the topic write must
        # wait for the lock, not delete the receipt as a crashed write
        before = self.head_state(refs=())
        real = memidx._atomic_write_bytes
        paused, release = threading.Event(), threading.Event()

        def pause_on_topic(path, data, *a, **kw):
            if Path(path).name == "foo.md":
                paused.set()
                release.wait(20)
            return real(path, data, *a, **kw)

        done = {}
        env = dict(os.environ, PYTHONPATH="", MEMCONTINUUM_TRANSCRIPTS_DIR=str(self.tmp / "tr"))
        with mock.patch.object(memidx, "_atomic_write_bytes", side_effect=pause_on_topic):
            first = threading.Thread(target=lambda: done.update(bind=self.bind()))
            first.start()
            try:
                self.assertTrue(paused.wait(20))
                other = subprocess.Popen(
                    [sys.executable, str(TOOLS_DIR / "memidx.py"), "unbind", "TOP-1", "src/other.py",
                     "--root", str(self.store)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env)
                time.sleep(0.6)
                self.assertIsNone(other.poll(), "the second command must wait for the lock")
                self.assertEqual(len(self.receipts()), 1)
            finally:
                release.set()
                first.join(30)
            _out, err = other.communicate(timeout=30)
        self.assertEqual(done["bind"][0], 0, done["bind"])
        self.assertEqual(other.returncode, 0, err)
        self.assertEqual(len(self.receipts()), 1)
        self.assertUndone(self.unbind(), before)

    def test_the_stack_changes_only_under_the_lock(self):
        self.head_state(refs=())
        scope = memidx._UndoScope(self.store, self.path, "TOP-1")
        d = self.rec_dir()
        d.mkdir(parents=True, exist_ok=True)
        junk = d / f"{scope.key}.00000007.json"
        junk.write_text("{not json")
        self.assertEqual(scope.stack(), [])
        self.assertTrue(junk.exists())  # reading never prunes
        with self.assertRaises(RuntimeError):
            scope.prune_invalid()
        with scope.locked():
            scope.prune_invalid()
        self.assertFalse(junk.exists())

    def test_a_receipt_that_cannot_be_written_durably_refuses_the_edit(self):
        # gate round 1 (Sol 4, Grok 3): no receipt, no edit -- never warn-and-proceed
        before = self.head_state(refs=())
        real = memidx._atomic_write_bytes

        def deny_receipts(path, data, *a, **kw):
            if Path(path).suffix == ".json":
                raise PermissionError(13, "Permission denied")
            return real(path, data, *a, **kw)

        with mock.patch.object(memidx, "_atomic_write_bytes", side_effect=deny_receipts):
            rc, out, err = self.bind()
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("the undo receipt could not be written durably at", err)
        self.assertIn(".json", err)
        self.assertIn("Permission denied", err)
        self.assertIn("nothing written", err)
        self.assertEqual((self.raw(), self.receipts()), (before, []))
        # a second edit on top of an existing receipt refuses the same way and keeps the first receipt
        self.assertEqual(self.bind()[0], 0)
        after = self.raw()
        with mock.patch.object(memidx, "_atomic_write_bytes", side_effect=deny_receipts):
            rc, out, err = self.bind("src/other.py")
        self.assertEqual(rc, 1, (out, err))
        self.assertEqual((self.raw(), len(self.receipts())), (after, 1))

    def test_a_failing_directory_fsync_refuses_the_edit(self):
        before = self.head_state(refs=())
        self.assertEqual(self.bind("src/other.py")[0], 0)  # the receipts directory exists now
        mid = self.raw()
        real = memidx._fsync_dir

        def fail_in_receipts(path):
            if Path(path) == self.rec_dir():
                raise OSError(5, "Input/output error")
            return real(path)

        with mock.patch.object(memidx, "_fsync_dir", side_effect=fail_in_receipts):
            rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("could not be written durably", err)
        self.assertIn("Input/output error", err)
        self.assertEqual(self.raw(), mid)
        self.assertNotEqual(mid, before)

    def test_a_read_only_receipts_directory_refuses_with_the_message(self):
        before = self.head_state(refs=())
        self.assertEqual(self.bind("src/other.py")[0], 0)
        mid = self.raw()
        d = self.rec_dir()
        scope = memidx._UndoScope(self.store, self.path, "TOP-1")
        d.chmod(0o500)
        try:
            rc, out, err = self.bind("src/foo.py")  # the lock file exists; the receipt cannot be created
            self.assertEqual(rc, 1, (out, err))
            self.assertIn("the undo receipt could not be written durably at", err)
            self.assertEqual(self.raw(), mid)
        finally:
            d.chmod(0o700)
        (d / (scope.key + ".lock")).unlink()
        d.chmod(0o500)
        try:
            rc, out, err = self.bind("src/foo.py")  # not even the lock file can be created
            self.assertEqual(rc, 1, (out, err))
            self.assertIn("cannot take the topic's receipt lock at", err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(self.raw(), mid)
        finally:
            d.chmod(0o700)

    def test_a_plan_made_on_one_image_is_not_applied_to_another(self):
        # the undo verdict ("no receipt applies") was reached on the bytes read under the lock; the command
        # then parses the file again. If the opposite command completed in between, writing would pair a
        # restore row with the pending removal: the write must refuse.
        before = self.bound_head()
        real = memidx._verified_topic_record
        nested = {}

        def after_the_plan(*a, **kw):
            if not nested:
                nested["rc"] = None
                nested["rc"] = self.unbind()[0]  # a competing command completes between plan and parse
            return real(*a, **kw)

        with mock.patch.object(memidx, "_verified_topic_record", side_effect=after_the_plan):
            rc, out, err = self.bind()
        self.assertEqual(nested["rc"], 0)
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("the topic changed while this command ran; nothing written", err)
        self.assertEqual(len(self.receipts()), 1)  # the competing unbind's, intact
        self.assertUndone(self.bind(), before)

    def test_a_held_lock_refuses_without_writing(self):
        before = self.head_state(refs=())
        scope = memidx._UndoScope(self.store, self.path, "TOP-1")
        self.rec_dir().mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.rec_dir() / (scope.key + ".lock")), os.O_CREAT | os.O_RDWR)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(memidx, "_LOCK_WAIT_SECONDS", 0.2):
                rc, out, err = self.bind()
        finally:
            os.close(fd)
        self.assertEqual(rc, 1)
        self.assertIn("holds the topic's receipt lock", err)
        self.assertEqual(self.raw(), before)

    def test_dry_run_leaves_no_receipt_and_undo_dry_run_keeps_it(self):
        before = self.head_state(refs=())
        rc, out, err = self.bind("src/foo.py", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.bind()[0], 0)
        after = self.raw()
        rc, out, err = self.unbind("src/foo.py", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run: nothing written", out)
        self.assertEqual(self.raw(), after)
        self.assertEqual(len(self.receipts()), 1)
        self.assertUndone(self.unbind(), before)

    def test_undo_binding_lists_and_undoes(self):
        before = self.head_state(refs=())
        rc, out, err = run_memidx(["undo-binding", "TOP-1", "--list", "--root", str(self.store)])
        self.assertEqual((rc, "no undo receipts" in out), (0, True), err)
        rc, out, err = run_memidx(["undo-binding", "TOP-1", "--root", str(self.store)])
        self.assertEqual(rc, 1)
        self.assertIn("no undo receipt applies", err)
        self.assertEqual(self.bind("src/other.py")[0], 0)
        bound_other = self.raw()
        self.assertEqual(self.bind("src/foo.py")[0], 0)
        rc, out, err = run_memidx(["undo-binding", "TOP-1", "--list", "--root", str(self.store)])
        self.assertEqual(rc, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("#2 bind src/foo.py"), lines)
        self.assertIn("baseline=entry ", lines[0])
        self.assertTrue(lines[1].startswith("#1 bind src/other.py"), lines)
        self.assertEqual(len(self.receipts()), 2)  # --list changes nothing
        for expected in (bound_other, before):  # after EVERY restoration, not only the last
            rc, out, err = run_memidx(["undo-binding", "TOP-1", "--root", str(self.store)])
            self.assertEqual(rc, 0, err)
            self.assertIn("undo: restored TOP-1", out)
            self.assertEqual(self.raw(), expected)
        self.assertEqual(self.receipts(), [])
        self.assertGuardsClean()

    def test_a_different_reference_or_the_same_direction_is_the_ordinary_path(self):
        before = self.head_state(refs=("src/other.py",))
        self.assertEqual(self.bind("src/foo.py")[0], 0)
        bound_foo = self.raw()
        rc, out, err = self.unbind("src/other.py")  # latest receipt is another ref: ordinary
        self.assertEqual(rc, 0, err)
        self.assertNotIn("undo:", out)
        self.assertEqual(len(self.receipts()), 2)
        # reverse order through the explicit command, B after EVERY restoration
        for expected in (bound_foo, before):
            self.assertEqual(run_memidx(["undo-binding", "TOP-1", "--root", str(self.store)])[0], 0)
            self.assertEqual(self.raw(), expected)

    def test_a_malformed_or_legacy_receipt_authorises_nothing(self):
        before = self.bound_head()
        scope = memidx._UndoScope(self.store, self.path, "TOP-1")
        d = self.rec_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{scope.key}.00000001.json").write_text("{not json")
        (d / f"{scope.key}.00000002.json").write_text(json.dumps({"v": 0, "command": "bind"}))
        rc, out, err = self.unbind()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("undo:", out)
        self.assertNotEqual(self.raw(), before)
        self.assertGuardsClean()

    def forge(self, **changes):
        """Rewrite the only receipt on disk with `changes` (a value of None deletes the key)."""
        (name,) = self.receipts()
        file = self.rec_dir() / name
        data = json.loads(file.read_text())
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        file.write_text(json.dumps(data))
        return file

    def test_a_receipt_that_is_not_wholly_valid_is_dropped_and_authorises_nothing(self):
        # gate round 1 (Sol 2): foreign topic id, foreign store, missing fields, invalid UTF-8
        import base64
        bad = {
            "foreign topic id": {"topic_id": "TOP-2"},
            "foreign store": {"store": "/somewhere/else"},
            "missing store": {"store": None},
            "missing mark key type": {"mark": {"ref": 1}},
            "invalid utf-8 before": {"before": base64.b64encode(b"\xff\xfe").decode()},
            "invalid utf-8 after": {"after": base64.b64encode(b"\xc3\x28").decode()},
            "unknown command": {"command": "rebind"},
            "committed state without its oid": {"committed": {"kind": "entry", "mode": "100644"}},
            "stage on the committed state": {"committed": {"kind": "absent", "stage": 0}},
            "staged entry without a stage": {"staged": {"kind": "entry", "mode": "100644", "oid": "ab"}},
            "version one": {"v": 1},
        }
        for name, changes in bad.items():
            with self.subTest(name):
                self.head_state(refs=())
                self.assertEqual(self.bind()[0], 0)
                after = self.raw()
                forged = self.forge(**changes)
                rc, out, err = self.unbind()
                self.assertEqual(rc, 0, err)
                self.assertNotIn("undo:", out)
                self.assertNotIn("Traceback", err)
                self.assertNotEqual(self.raw(), after)
                # the forged file was dropped; the only receipt left is the ordinary unbind's own
                self.assertEqual(json.loads(forged.read_text())["command"], "unbind")
                self.assertEqual(len(self.receipts()), 1)
                commit_all(self.store)
                for leftover in self.rec_dir().glob("*.json"):
                    leftover.unlink()

    def test_receipts_are_outside_the_store_tree_and_the_working_tree_stays_clean(self):
        self.head_state(refs=())
        self.assertEqual(self.bind()[0], 0)
        status = git(["status", "--porcelain", "--untracked-files=all"], self.store).stdout
        self.assertEqual(status.split(), ["M", "topics/foo.md"])
        self.assertIn(".git", self.rec_dir().parts)

    def test_a_store_outside_git_has_no_receipts_and_behaves_as_before(self):
        store = self.tmp / "plain"
        (store / "topics").mkdir(parents=True)
        topic = store / "topics" / "foo.md"
        topic.write_text(binding_topic(code_refs=("src/other.py",)))
        run = lambda *a: run_memidx([*a, "--root", str(store)])  # noqa: E731
        self.assertEqual(run("bind", "TOP-1", "src/foo.py", "--code-root", str(self.code))[0], 0)
        rc, out, err = run("unbind", "TOP-1", "src/foo.py")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("undo:", out)
        self.assertIn("remove", topic.read_text())

    def test_a_bare_repository_gets_no_receipts(self):
        bare = self.tmp / "bare.git"
        git(["init", "-q", "--bare", str(bare)], self.tmp)
        (bare / "topics").mkdir()
        topic = bare / "topics" / "foo.md"
        topic.write_text(binding_topic(code_refs=("src/other.py",)))
        self.assertFalse(memidx._UndoScope(bare, topic, "TOP-1").enabled)
        rc, out, err = run_memidx(["bind", "TOP-1", "src/foo.py", "--root", str(bare), "--code-root", str(self.code)])
        self.assertEqual(rc, 0, err)
        self.assertFalse((bare / "memcontinuum").exists())
        rc, out, err = run_memidx(["unbind", "TOP-1", "src/foo.py", "--root", str(bare)])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("undo:", out)

    def test_ambient_git_variables_cannot_redirect_the_receipt_probes(self):
        before = self.head_state(refs=())
        other = git_store(self.tmp / "elsewhere")
        env = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
               "GIT_INDEX_FILE": str(other / ".git" / "index")}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.bind()[0], 0)
        self.assertEqual(len(self.receipts()), 1)  # in THIS store's git directory
        with mock.patch.dict(os.environ, env):
            rc, out, err = self.unbind()
        self.assertEqual(rc, 0, err)
        self.assertIn("undo: restored", out)
        self.assertEqual(self.raw(), before)
        self.assertEqual(list((other / ".git").glob("memcontinuum*")), [])
        self.assertGuardsClean()

    # ---- gate round 2 (TOP-0137 L39) -------------------------------------------------------

    def _patch_run(self, match, result):
        """subprocess.run in memidx behaves as before except for the git call whose argv contains
        `match`, which raises/returns `result`."""
        real = subprocess.run

        def fake(argv, *a, **kw):
            if match in argv:
                if isinstance(result, BaseException):
                    raise result
                return subprocess.CompletedProcess(argv, result[0], result[1], "")
            return real(argv, *a, **kw)

        return mock.patch.object(memidx.subprocess, "run", side_effect=fake)

    def test_work_tree_detection_has_three_results(self):
        probe = "--is-inside-work-tree"
        self.assertEqual(memidx._work_tree_state(self.store), "yes")
        plain = self.tmp / "plain-detect"
        plain.mkdir()
        self.assertEqual(memidx._work_tree_state(plain), "no")  # git itself answers 128
        for name, result, expect in (
                ("false", (0, "false\n"), "no"),
                ("exit 128", (128, ""), "no"),
                ("exit 1", (1, ""), None),
                ("empty output", (0, ""), None),
                ("garbage", (0, "maybe\n"), None),
                ("timeout", subprocess.TimeoutExpired(["git"], 30), None),
        ):
            with self.subTest(name), self._patch_run(probe, result):
                self.assertEqual(memidx._work_tree_state(self.store), expect)
        with self._patch_run(probe, FileNotFoundError(2, "git")):
            self.assertEqual(memidx._work_tree_state(plain), "no")  # git missing, no .git entry
            self.assertIsNone(memidx._work_tree_state(self.store))  # git missing where a .git entry exists

    def test_an_unanswered_work_tree_probe_refuses_and_writes_nothing(self):
        before = self.head_state(refs=())
        with self._patch_run("--is-inside-work-tree", subprocess.TimeoutExpired(["git"], 30)):
            rc, out, err = self.bind()
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("git state unavailable for topics/foo.md (work-tree detection); nothing written", err)
        self.assertEqual((self.raw(), self.receipts()), (before, []))
        self.assertEqual(self.bind()[0], 0)  # git answers again: an ordinary, receipted bind
        after = self.raw()
        self.assertEqual(len(self.receipts()), 1)
        with self._patch_run("--is-inside-work-tree", (1, "")):
            rc, out, err = self.unbind()
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("(work-tree detection)", err)
        self.assertEqual((self.raw(), len(self.receipts())), (after, 1))
        self.assertUndone(self.unbind(), before)

    def test_a_failed_receipt_path_lookup_inside_a_work_tree_refuses(self):
        before = self.head_state(refs=())
        real = memidx._git_run

        def lookup_fails(store, args, data=None):
            return None if "--git-path" in args else real(store, args, data)

        with mock.patch.object(memidx, "_git_run", side_effect=lookup_fails):
            rc, out, err = self.bind()
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("(receipt directory lookup); nothing written", err)
        self.assertEqual((self.raw(), self.receipts()), (before, []))

    def test_a_verified_non_work_tree_keeps_the_ordinary_path_even_without_git(self):
        store = self.tmp / "plain-nogit"
        (store / "topics").mkdir(parents=True)
        topic = store / "topics" / "foo.md"
        topic.write_text(binding_topic(code_refs=("src/other.py",)))
        run = lambda *a: run_memidx([*a, "--root", str(store)])  # noqa: E731
        with self._patch_run("--is-inside-work-tree", FileNotFoundError(2, "git")):
            self.assertEqual(run("bind", "TOP-1", "src/foo.py", "--code-root", str(self.code))[0], 0)
            rc, out, err = run("unbind", "TOP-1", "src/foo.py")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("undo:", out)
        self.assertIn("remove", topic.read_text())

    def test_a_linked_worktree_keeps_its_own_receipts(self):
        self.head_state(refs=())
        git(["worktree", "add", "-q", str(self.tmp / "wt")], self.store)
        wt = self.tmp / "wt"
        before = (wt / "topics" / "foo.md").read_bytes()
        run = lambda *a: run_memidx([*a, "--root", str(wt)])  # noqa: E731
        self.assertEqual(run("bind", "TOP-1", "src/foo.py", "--code-root", str(self.code))[0], 0)
        wt_dir = self.rec_dir(wt)
        self.assertNotEqual(wt_dir, self.rec_dir())
        self.assertEqual(len(list(wt_dir.glob("*.json"))), 1)
        self.assertEqual(self.receipts(), [])
        rc, out, err = run("unbind", "TOP-1", "src/foo.py")
        self.assertEqual(rc, 0, err)
        self.assertIn("undo: restored TOP-1", out)
        self.assertEqual((wt / "topics" / "foo.md").read_bytes(), before)
        errors, _ = memlint.lint_root(wt)
        self.assertEqual(errors, [], errors)
        rc, out = run_memlint(["--against-ref", "HEAD", str(wt)])
        self.assertEqual(rc, 0, out)


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
