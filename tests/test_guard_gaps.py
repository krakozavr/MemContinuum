"""0.6.0 hygiene PR -- the frontmatter guard gaps (TOP-0137 L26): `code_refs:`
membership under the guard through the frozen `ref_marks:` rows, `bind`
writing its row and restoring an unbound ref, `unbind`, `type` frozen, and the
anchor rule's "unbound" clause. Every fixture builds its own throwaway store."""
import contextlib
import io
import json
import os
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
    def test_a_type_change_on_a_recorded_topic_is_refused(self):
        base = ttext(["src/a.py"])
        self.store(base)
        self.assertClean(base)
        self.assertRefused(base.replace("type: topic\n", "type: investigation\n"),
                           "type changed 'topic' -> 'investigation'")

    def test_dropping_the_type_is_a_change_too(self):
        base = ttext(["src/a.py"])
        self.store(base)
        self.assertRefused(base.replace("type: topic\n", ""), "type changed 'topic' -> None")

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

    def test_a_schema_invalid_but_strict_ref_keeps_its_type_frozen(self):
        # REF: valid YAML, `type: topic`, `tags: not-a-list` -> valid=False, fallback=False
        broken = ttext(["src/a.py"]).replace("tags: []\n", "tags: not-a-list\n", 1)
        res = memidx.parse_record_text(broken, "x")
        self.assertFalse(res.valid, res)
        self.assertFalse(res.fallback, res)
        self.store(broken)
        repaired = broken.replace("tags: not-a-list\n", "tags: []\n", 1)
        self.assertClean(repaired)
        self.assertRefused(repaired.replace("type: topic\n", "type: incident\n", 1),
                           "type changed 'topic' -> 'incident'")

    def test_a_fallback_ref_with_a_clean_known_type_keeps_it_frozen(self):
        broken = self._bad_title(ttext(["src/a.py"]))
        self.assertTrue(memidx.parse_record_text(broken, "x").fallback)
        self.store(broken)
        fixed = broken.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed)
        self.assertRefused(fixed.replace("type: topic\n", "type: incident\n", 1),
                           "type changed 'topic' -> 'incident'")

    def test_a_fallback_ref_with_a_garbage_recovered_type_is_not_frozen(self):
        broken = self._bad_title(ttext(["src/a.py"]), type_line="type: banana # note")
        res = memidx.parse_record_text(broken, "x")
        self.assertTrue(res.fallback, res)
        self.store(broken)
        fixed = broken.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace("type: banana # note", "type: topic"))

    def test_a_new_side_quoted_hash_value_is_not_stripped_into_the_old_type(self):
        # gate round 3 R3-1: REF fallback with recovered `topic`; NEW still fallback-parsed with
        # `type: "topic # actual value"` -- no strip may turn that into `topic`
        broken = self._bad_title(ttext(["src/a.py"]))
        self.assertTrue(memidx.parse_record_text(broken, "x").fallback)
        self.store(broken)
        # no canonical marker left (no id, links, or bare `type: topic`): a tolerated fallback note
        new = '---\ntype: "topic # actual value"\ntitle: bad: title\n---\n\nBody.\n'
        res = memidx.parse_record_text(new, "x")
        self.assertTrue(res.fallback and res.valid, res)
        self.assertRefused(new, "type changed 'topic' -> 'topic # actual value'")

    # gate round 4 R4-1 / round 6 R6-1 / round 8 R8-1: the frozen REF value is the engine's kind reading; the NEW decoded type must equal it
    # (gate round 8 R8-1: the REF carries a TOP- id, so the engine's kind reading is `topic` through the id prefix)
    ODD_REF = '---\nid: TOP-0001\ntype: "topic # actual value"\ntitle: bad: title\n---\n\nBody.\n'

    def _odd_ref(self):
        res = memidx.parse_record_text(self.ODD_REF, "x")
        self.assertTrue(res.fallback, res)   # (the id makes the record schema-invalid; the type freeze does not care)
        self.assertEqual(res.frontmatter.get("type"), "topic # actual value")
        self.store(self.ODD_REF)

    def test_an_unchanged_odd_type_is_refused_with_the_normalise_message_on_a_title_only_repair(self):
        self._odd_ref()
        self.assertRefused(self.ODD_REF.replace("title: bad: title", 'title: "bad: title"'),
                           "type changed 'topic' -> 'topic # actual value'", "a repair of a malformed record must declare type: topic")

    def test_an_unchanged_odd_type_is_refused_with_the_normalise_message_on_a_body_only_edit(self):
        self._odd_ref()
        self.assertRefused(self.ODD_REF.replace("Body.", "Body, edited."),
                           "type changed 'topic' -> 'topic # actual value'", "a repair of a malformed record must declare type: topic")

    def test_an_odd_ref_type_may_become_its_bare_token(self):
        self._odd_ref()
        fixed = self.ODD_REF.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace('type: "topic # actual value"', "type: topic"))

    def test_an_odd_ref_type_changed_to_another_type_is_refused(self):
        self._odd_ref()
        self.assertRefused(self.ODD_REF.replace('type: "topic # actual value"', "type: incident"),
                           "type changed 'topic' -> 'incident'")

    # gate round 5 R5-1 / round 6 R6-1: a raw fallback spelling is never compared with a decoded
    # strict value; the NEW record's decoded type must equal the bare token (no source-line rule)
    def _fallback_ref(self, type_line, valid=True, rid="TOP-0001"):
        ref = f"---\nid: {rid}\n{type_line}\ntitle: bad: title\n---\n\nBody.\n"
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(res.fallback, res)   # (validity is irrelevant to the type freeze; the TOP- id makes it invalid)
        self.store(ref)
        return ref

    def _title_repaired(self, ref):
        return ref.replace("title: bad: title", 'title: "bad: title"')

    def test_a_single_quoted_odd_type_with_a_doubled_quote_is_refused_with_the_normalise_message(self):
        ref = self._fallback_ref("type: 'topic # it''s x'")
        fixed = self._title_repaired(ref)
        self.assertEqual(memidx.parse_record_text(fixed, "x").frontmatter.get("type"), "topic # it's x")
        self.assertRefused(fixed, "type changed 'topic' -> \"topic # it's x\"", "a repair of a malformed record must declare type: topic")

    def test_a_double_quoted_odd_type_is_refused_with_the_normalise_message(self):
        ref = self._fallback_ref('type: "topic # x"')
        self.assertRefused(self._title_repaired(ref), "type changed 'topic' -> 'topic # x'", "a repair of a malformed record must declare type: topic")

    def test_a_double_quoted_odd_type_with_an_escape_is_refused_with_the_normalise_message(self):
        ref = self._fallback_ref('type: "topic # \\u0078"')
        self.assertRefused(self._title_repaired(ref), "type changed 'topic' -> 'topic # x'", "a repair of a malformed record must declare type: topic")

    def test_an_odd_type_line_with_changed_spacing_is_refused_with_the_normalise_message(self):
        ref = self._fallback_ref("type: 'topic # it''s x'")
        self.assertRefused(self._title_repaired(ref).replace("type: 'topic", "type:   'topic"),
                           "type changed 'topic' -> \"topic # it's x\"", "a repair of a malformed record must declare type: topic")

    def test_the_odd_single_quoted_ref_may_become_bare_topic_or_is_refused_for_incident(self):
        ref = self._fallback_ref("type: 'topic # it''s x'")
        fixed = self._title_repaired(ref)
        self.assertClean(fixed.replace("type: 'topic # it''s x'", "type: topic"))
        self.assertRefused(fixed.replace("type: 'topic # it''s x'", "type: incident"),
                           "type changed 'topic' -> 'incident'")

    def test_an_odd_type_line_respelled_to_a_different_literal_is_refused(self):
        ref = self._fallback_ref("type: 'topic # it''s x'")
        self.assertRefused(self._title_repaired(ref).replace("it''s x", "other"),
                           "type changed 'topic' -> 'topic # other'")

    # gate round 5 R5-2: a quoted type followed by a comment is frozen too
    def test_a_quoted_type_followed_by_a_comment_is_frozen(self):
        ref = self._fallback_ref('type: "incident" # note', rid="INC-0001")   # round 8: an INC- id, so the kind is incident
        self.assertEqual(memidx.parse_record_text(ref, "x").frontmatter.get("type"), 'incident" # note')
        fixed = self._title_repaired(ref)
        self.assertClean(fixed)
        self.assertRefused(fixed.replace('type: "incident" # note', "type: concept"),
                           "type changed 'incident' -> 'concept'")
        self.assertClean(fixed.replace('type: "incident" # note', "type: incident"))

    # gate round 8 R8-1: the frozen kind of a fallback REF is the engine's own `_record_kind` reading
    def test_record_kind_table_for_a_fallback_ref(self):
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

    # gate round 8 R8-1 (Sol MAJOR/MINOR, Grok): spellings of a canonical type that the fallback
    # garbles all freeze through the id prefix (or the links list), never through a string reading
    def _kind_refused_and_declared(self, type_line, kind="topic", rid="TOP-0001"):
        ref = self._fallback_ref(type_line, rid=rid)
        fixed = self._title_repaired(ref)
        other = "investigation" if kind != "investigation" else "concept"
        self.assertRefused(fixed.replace(type_line, f"type: {other}"), f"type changed '{kind}' -> '{other}'")
        self.assertClean(fixed.replace(type_line, f"type: {kind}"))

    def test_a_double_quoted_canonical_type_is_frozen_through_the_id_prefix(self):
        self._kind_refused_and_declared('type: "topic"')

    def test_an_escaped_canonical_type_is_frozen_through_the_id_prefix(self):
        # Sol's round 8 MAJOR: "to\u0070ic" and "\x74opic" decode to `topic`
        self._kind_refused_and_declared('type: "to\\u0070ic"')
        self._kind_refused_and_declared('type: "\\x74opic"')

    def test_a_tag_prefixed_canonical_type_is_frozen_through_the_id_prefix(self):
        self._kind_refused_and_declared('type: !!str "topic"')
        self._kind_refused_and_declared("type: !!str 'topic'")

    def test_a_block_scalar_canonical_type_is_frozen_through_the_id_prefix(self):
        self._kind_refused_and_declared("type: >-\n  topic")
        self._kind_refused_and_declared("type: |\n  topic")

    def test_an_unquoted_quote_comment_type_must_be_declared_topic_by_the_repair(self):
        # Sol's round 8 MINOR: `topic"# note` is not canonical YAML, but the TOP- id gives kind topic;
        # a title-only repair keeping that spelling is refused with the normalise message
        ref = self._fallback_ref('type: topic"# note')
        fixed = self._title_repaired(ref)
        self.assertRefused(fixed, "type changed 'topic'", "a repair of a malformed record must declare type: topic")
        self.assertClean(fixed.replace('type: topic"# note', "type: topic"))

    def test_a_garbled_type_with_an_inc_id_freezes_incident(self):
        ref = self._fallback_ref("type: banana # note", rid="INC-0001")
        fixed = self._title_repaired(ref)
        self.assertRefused(fixed.replace("type: banana # note", "type: topic"),
                           "type changed 'incident' -> 'topic'", "a repair of a malformed record must declare type: incident")
        self.assertRefused(fixed.replace("type: banana # note", ""), "type removed (frozen 'incident')")
        self.assertClean(fixed.replace("type: banana # note", "type: incident"))

    def test_a_garbled_type_with_no_id_and_no_links_is_exempt(self):
        # kind None: nothing is recoverable, so nothing is frozen (documented exemption)
        ref = "---\ntype: banana # note\ntitle: bad: title\n---\n\nBody.\n"
        self.assertEqual(memlint._record_kind(memidx.parse_record_text(ref, "x").frontmatter), None)
        self.store(ref)
        fixed = self._title_repaired(ref)
        self.assertClean(fixed.replace("type: banana # note", "type: investigation"))
        self.assertClean(fixed.replace("type: banana # note\n", ""))

    def test_a_no_space_hash_after_a_quoted_canonical_type_is_frozen(self):
        # Sol's fixture: PyYAML reads `"topic"# note` as `topic`
        ref = self._fallback_ref('type: "topic"# note')
        self.assertEqual(memidx.parse_record_text(ref, "x").frontmatter.get("type"), 'topic"# note')
        fixed = self._title_repaired(ref)
        self.assertEqual(memidx.parse_record_text(fixed, "x").frontmatter.get("type"), "topic")
        self.assertClean(fixed)
        self.assertRefused(fixed.replace('type: "topic"# note', "type: investigation"),
                           "type changed 'topic' -> 'investigation'")

    def test_a_no_space_hash_repair_keeping_type_topic_is_clean(self):
        ref = self._fallback_ref('type: "topic"# note')
        self.assertClean(self._title_repaired(ref).replace('type: "topic"# note', "type: topic"))

    # gate round 6 R6-1: the effective key is the YAML-decoded one, never a scanned source line
    def test_a_duplicate_type_key_that_wins_last_is_refused(self):
        ref = self._fallback_ref("type: topic", valid=False)
        fixed = self._title_repaired(ref).replace("type: topic\n", "type: topic\ntype: incident\n", 1)
        self.assertEqual(memidx.parse_record_text(fixed, "x").frontmatter.get("type"), "incident")
        self.assertRefused(fixed, "type changed 'topic' -> 'incident'")

    def test_a_block_scalar_decoy_type_line_does_not_hide_a_changed_real_type(self):
        ref = "---\nnote: |\n  type: topic\ntype: topic\ntitle: bad: title\n---\n\nBody.\n"
        self.assertTrue(memidx.parse_record_text(ref, "x").fallback)
        self.store(ref)
        fixed = self._title_repaired(ref)
        self.assertClean(fixed)
        self.assertRefused(fixed.replace("\ntype: topic\ntitle", "\ntype: incident\ntitle"),
                           "type changed 'topic' -> 'incident'")

    def test_a_nested_meta_type_above_the_real_type_does_not_govern(self):
        ref = self._fallback_ref("meta:\n  type: unrelated\ntype: 'topic # it''s x'")
        fixed = self._title_repaired(ref)
        self.assertRefused(fixed, "type changed 'topic' -> \"topic # it's x\"", "a repair of a malformed record must declare type: topic")
        self.assertClean(fixed.replace("type: 'topic # it''s x'", "type: topic"))
        self.assertRefused(fixed.replace("type: 'topic # it''s x'", "type: incident"),
                           "type changed 'topic' -> 'incident'")

    def test_an_empty_new_mapping_loses_a_frozen_type(self):
        # gate round 3 R3-2: the type may not disappear by emptying the frontmatter
        base = ttext(["src/a.py"])
        self.store(base)
        empty = "---\n{}\n---\n\nBody.\n"
        self.assertEqual(memidx.parse_record_text(empty, "x").frontmatter, {})
        self.assertRefused(empty, "type removed (frozen 'topic')")

    def test_an_empty_new_mapping_loses_a_fallback_refs_trusted_type(self):
        broken = self._bad_title(ttext(["src/a.py"]))
        self.assertTrue(memidx.parse_record_text(broken, "x").fallback)
        self.store(broken)
        self.assertRefused("---\n{}\n---\n\nBody.\n", "type removed (frozen 'topic')")

    def test_a_new_side_with_other_keys_but_no_type_is_a_removal_too(self):
        self.store(ttext(["src/a.py"]))
        self.assertRefused("---\ntitle: x\n---\n\nBody.\n", "type removed (frozen 'topic')")

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
        # adjusted in gate round 3 R3-3: a REF record that recorded NO `type` has nothing frozen
        # (a recorded non-topic type is now frozen too, see the R3-3 tests below)
        note = "---\nid: INC-9001\ntitle: x\n---\n\nBody.\n"
        self.store(note)
        self.assertClean(note.replace("id: INC-9001\n", "id: INC-9001\ntype: investigation\n"))

    def _non_topic_ref(self, kind):
        base = ttext(["src/a.py"]).replace("type: topic\n", f"type: {kind}\n", 1)
        broken = base.replace("tags: []\n", "tags: not-a-list\n", 1)
        res = memidx.parse_record_text(broken, "x")
        self.assertFalse(res.valid, res)
        self.assertFalse(res.fallback, res)
        self.store(broken)
        return broken

    def test_a_non_topic_ref_with_a_frozen_type_keeps_it_through_a_repair_incident(self):
        broken = self._non_topic_ref("incident")
        repaired = broken.replace("tags: not-a-list\n", "tags: []\n", 1)
        self.assertClean(repaired)
        self.assertRefused(repaired.replace("type: incident\n", "type: investigation\n", 1),
                           "type changed 'incident' -> 'investigation'")

    def test_a_non_topic_ref_with_a_frozen_type_keeps_it_through_a_repair_concept(self):
        broken = self._non_topic_ref("concept")
        repaired = broken.replace("tags: not-a-list\n", "tags: []\n", 1)
        self.assertClean(repaired)
        self.assertRefused(repaired.replace("type: concept\n", "type: investigation\n", 1),
                           "type changed 'concept' -> 'investigation'")

    def test_a_non_topic_ref_with_no_type_is_free_to_gain_one(self):
        note = "---\ntitle: just a note\n---\n\nBody.\n"
        self.store(note)
        self.assertClean(note.replace("title: just a note\n", "title: just a note\ntype: incident\n"))

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


# ---------------------------------------------------------------------------
# one block-item deletion shared by unbind and mark-standing
# ---------------------------------------------------------------------------

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
        rc, _o, err = self.bind("src/lib/")
        self.assertEqual(rc, 2, err)  # bind still refuses a prefix

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
