"""0.6.0 filing command (TOP-0137 L23): `record`, `append`, `promote` -- pure line
insertion proven by memlint's append-only guard, authority derived from the
session transcript, advisory dedupe, retrieval check, one log receipt. Every
fixture builds its own throwaway git store."""
import contextlib
import hashlib
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

import yaml

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

NOW = datetime(2026, 11, 7, 15, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
SID = "11111111-2222-3333-4444-555555555555"
OWNER_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
YES_UUID = "aaaaaaaa-0000-0000-0000-000000000002"
TOOL_UUID = "aaaaaaaa-0000-0000-0000-000000000003"
META_UUID = "aaaaaaaa-0000-0000-0000-000000000004"
OWNER_TEXT = "I told you already to make manual wiring the only official way  and document it"

TOPIC = """---
type: topic
id: TOP-9100
title: Filing fixture topic about widget caching
area: memory
project: p
current: L2
code_refs: []
tags: [fixture]
links:
  - link: L2
    date: 2026-11-01
    status: active
    kind: amended
    reverses: L1
    reason_for_change: changed-context
    ruling:
      text: "the second ruling: widgets are cached"
      authority: owner-ratified
      source: "owner, 2026-11-01"
    rationale:
      text: "because speed"
      authority: agent-inference
    alternatives:
      - {option: "no cache", rejected_because: "slow", authority: agent-inference}
    recorded_by: fable
    recorded_at: 2026-11-01
  - link: L1
    date: 2026-10-01
    status: active
    kind: adopted
    ruling:
      text: "the first ruling"
      authority: agent-inference
      source: s
    recorded_by: fable
    recorded_at: 2026-10-01
---

Body of the fixture topic.
"""

PROVISIONAL_TOPIC = """---
type: topic
id: TOP-9101
title: Provisional fixture
area: memory
project: p
tags: []
links:
  - link: L1
    date: 2026-10-01
    status: provisional
    kind: adopted
    ruling:
      text: "an inference awaiting the owner"
      authority: agent-inference
      source: s
    rationale:
      text: "the reasoning"
      authority: agent-inference
    recorded_by: fable
    recorded_at: 2026-10-01
---

Body.
"""


def git(args, cwd):
    return subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, text=True, check=True)


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_memidx(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = memidx.main(args)
    return rc, out.getvalue(), err.getvalue()


def run_memlint(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = memlint.main(args)
    return rc, buf.getvalue()


class FilingCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.store = self.tmp / "store"
        (self.store / "topics" / "memory").mkdir(parents=True)
        git(["init", "-q", str(self.store)], self.store)
        git(["config", "user.email", "t@example.com"], self.store)
        git(["config", "user.name", "T"], self.store)
        self.topic = self.store / "topics" / "memory" / "widget.md"
        self.topic.write_text(TOPIC)
        self.prov = self.store / "topics" / "memory" / "prov.md"
        self.prov.write_text(PROVISIONAL_TOPIC)
        self.commit("base")
        self.head0 = self.head()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.transcripts = self.tmp / "transcripts"
        (self.transcripts / "proj").mkdir(parents=True)
        self.db = self.tmp / "p.sqlite"
        env = mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home),
                                           "MEMCONTINUUM_TRANSCRIPTS_DIR": str(self.transcripts)})
        env.start()
        self.addCleanup(env.stop)
        now = mock.patch.object(memidx, "_now", return_value=NOW)
        now.start()
        self.addCleanup(now.stop)
        self.addCleanup(self.assert_head_unchanged)  # the command never commits (design 7.9)

    def commit(self, msg):
        git(["add", "-A"], self.store)
        git(["commit", "-q", "-m", msg], self.store)

    def head(self):
        return git(["rev-parse", "HEAD"], self.store).stdout.strip()

    def assert_head_unchanged(self):
        self.assertEqual(self.head(), self.head0)

    def write_transcript(self, name="proj", sid=SID, cwd="/work/p", extra=()):
        lines = [
            {"type": "user", "uuid": META_UUID, "isMeta": True, "sessionId": sid, "cwd": cwd,
             "timestamp": "2026-11-07T20:00:00.000Z", "message": {"role": "user", "content": "meta text"}},
            {"type": "user", "uuid": OWNER_UUID, "sessionId": sid, "cwd": cwd,
             "timestamp": "2026-11-07T20:01:00.000Z", "message": {"role": "user", "content": OWNER_TEXT}},
            {"type": "user", "uuid": TOOL_UUID, "sessionId": sid, "cwd": cwd,
             "timestamp": "2026-11-07T20:02:00.000Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "content": OWNER_TEXT}]}},
            {"type": "user", "uuid": YES_UUID, "sessionId": sid, "cwd": cwd,
             "timestamp": "2026-11-07T20:03:00.000Z", "message": {"role": "user", "content": "ok, agreed."}},
        ] + list(extra)
        d = self.transcripts / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    def run_cmd(self, *argv):
        base = ["--root", str(self.store), "--project", "p", "--db", str(self.db)]
        return run_memidx(list(argv) + base)

    def append(self, *flags, topic="TOP-9100", ruling="a new ruling"):
        return self.run_cmd("append", topic, "--ruling", ruling, *flags)

    def parsed_links(self, text):
        return memidx.parse_record_raw(text, "x").frontmatter["links"]

    def spans(self, text):
        return memlint._link_raw_spans(text, self.parsed_links(text))

    def _guard_error(self, rel):
        full = str(self.store.resolve() / rel)
        return mock.patch.object(memlint, "check_append_only",
                                 return_value=([f"{full}: link L1 body changed"], 1, []))

    def log_lines(self):
        p = self.home / "hook.log"
        return [l for l in p.read_text().splitlines() if " filing " in l] if p.exists() else []


class TestByteIdentity(FilingCase):
    def test_append_keeps_every_old_link_byte_identical(self):
        old = self.topic.read_text()
        rc, out, err = self.append("--rationale", "why: because # reasons", "--evidence", "tests/x.py",
                                   "--alternative", "do nothing || it rots", "--revisit-if", "it breaks")
        self.assertEqual(rc, 0, err)
        new = self.topic.read_text()
        old_spans, new_spans = self.spans(old), self.spans(new)
        for lid, span in old_spans.items():
            self.assertEqual(new_spans[lid], span)
        self.assertEqual(sorted(new_spans), ["L1", "L2", "L3"])
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, text)
        # pure insertion: the diff removes only the `current:` line
        removed = [l for l in out.splitlines() if l.startswith("-") and not l.startswith("---")]
        self.assertEqual(removed, ["-current: L2"])
        link = self.parsed_links(new)[0]
        self.assertEqual(link["link"], "L3")
        self.assertEqual(link["alternatives"][0]["authority"], "agent-inference")
        self.assertIn("TOP-9100 L3: a new ruling", out)

    def test_current_moves_only_when_the_new_link_is_active(self):
        rc, _o, err = self.append("--status", "provisional")
        self.assertEqual(rc, 0, err)
        self.assertIn("current: L2", self.topic.read_text())
        self.assertEqual(run_memlint(["--against-ref", "HEAD", str(self.store)])[0], 0)

    def test_mutation_control_a_dump_fails_the_guard(self):
        """A yaml.safe_dump round-trip of the SAME file is rejected by
        `--against-ref`; with the byte comparison disabled it slips through, so
        the tests depend on byte checking, not on the command's word."""
        rc, _o, err = self.append()
        self.assertEqual(rc, 0, err)
        written = self.topic.read_text()
        self.assertEqual(run_memlint(["--against-ref", "HEAD", str(self.store)])[0], 0)
        parsed = memidx.parse_record_raw(written, "x")
        dumped = "---\n" + yaml.safe_dump(parsed.frontmatter, sort_keys=False, allow_unicode=True) + "---\n" + parsed.body
        self.topic.write_text(dumped)
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertNotEqual(rc, 0, text)
        self.assertIn("reformatted, not appended", text)
        with mock.patch.object(memlint, "_link_bytes_changed", return_value=False):
            rc2, text2 = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc2, 0, text2)


class TestRefusals(FilingCase):
    def assert_refused(self, rc, err, needle=None, files=()):
        self.assertEqual(rc, 1, err)
        if needle:
            self.assertIn(needle, err)
        for path, digest in files:
            self.assertEqual(sha(path), digest, f"{path} changed")

    def test_refusals_leave_bytes_unchanged(self):
        flow = self.store / "topics" / "memory" / "flow.md"
        flow.write_text("---\ntype: topic\nid: TOP-9102\ntitle: Flow\narea: memory\nlinks: []\n---\n\nB\n")
        binary = self.store / "topics" / "memory" / "bin.md"
        binary.write_bytes(b"---\nid: TOP-9103\ntype: topic\ntitle: \xff\xfe\n---\n")
        dup_a = self.store / "topics" / "memory" / "dupa.md"
        dup_b = self.store / "topics" / "memory" / "dupb.md"
        for p in (dup_a, dup_b):
            p.write_text(PROVISIONAL_TOPIC.replace("TOP-9101", "TOP-9104"))
        files = [(p, sha(p)) for p in (self.topic, self.prov, flow, binary, dup_a, dup_b)]
        cases = [
            (["append", "TOP-9100", "--ruling", "x", "--authority", "owner-verbatim"], "derived"),
            (["append", "TOP-9100", "--ruling", "x", "--authority", "owner-ratified"], "derived"),
            (["append", "TOP-0000", "--ruling", "x"], "no topic"),
            (["append", "TOP-9100", "--ruling", "x", "--reverses", "L9", "--reason-for-change", "changed-mind"], "names no link"),
            (["append", "TOP-9100", "--ruling", "x", "--reverses", "L1"], "go together"),
            (["append", "TOP-9100", "--ruling", "x", "--reason-for-change", "changed-mind"], "go together"),
            (["append", "TOP-9100", "--ruling", "x", "--kind", "reversed"], "requires --reverses"),
            (["append", "TOP-9100", "--ruling", "x", "--kind", "restored"], "requires --reverses"),
            (["append", "TOP-9102", "--ruling", "x"], "not a block list"),
            (["append", "TOP-9103", "--ruling", "x"], "UTF-8"),
            (["append", "TOP-9104", "--ruling", "x"], "carried by 2 files"),
            (["append", "TOP-9100", "--ruling", "   "], "must not be empty"),
            (["append", "TOP-9100", "--ruling", "x", "--alternative", "no separator"], "OPTION"),
            (["append", "TOP-9100", "--ruling", "x", "--owner-message", OWNER_UUID], "--session"),
            (["append", "TOP-9100", "--ruling", "x", "--kind", "reversed", "--reverses", "L1",
              "--reason-for-change", "changed-mind"], "still active"),
            (["promote", "TOP-9100", "L2", "--session", SID, "--yes-message", YES_UUID], "not promotable"),
            (["promote", "TOP-9101", "L9", "--session", SID, "--yes-message", YES_UUID], "no link L9"),
            (["promote", "TOP-9101", "L1", "--session", SID], "needs --owner-message"),
        ]
        self.write_transcript()
        for argv, needle in cases:
            with self.subTest(argv=argv):
                rc, _o, err = self.run_cmd(*argv)
                self.assert_refused(rc, err, None if needle == "not promotable" else needle, files)
                if needle == "not promotable":
                    self.assertIn("promotes an agent-inference", err)
        for p, d in files:
            self.assertEqual(sha(p), d)

    def test_dry_run_writes_nothing(self):
        before = sha(self.topic)
        rc, out, err = self.append("--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run: nothing written", out)
        self.assertEqual(sha(self.topic), before)
        self.assertFalse(self.db.exists())  # no reindex on a dry run
        rc, out, err = self.run_cmd("record", "--title", "New topic", "--area", "memory",
                                    "--ruling", "r", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.store / "topics" / "memory" / "new-topic.md").exists())

    def test_unresolved_commit_citation_refused_when_code_root_given(self):
        code = self.tmp / "code"
        code.mkdir()
        git(["init", "-q", str(code)], code)
        git(["config", "user.email", "t@example.com"], code)
        git(["config", "user.name", "T"], code)
        (code / "f.txt").write_text("x")
        git(["add", "-A"], code)
        git(["commit", "-q", "-m", "c1"], code)
        good = git(["rev-parse", "--short", "HEAD"], code).stdout.strip()
        before = sha(self.topic)
        rc, _o, err = self.append("--code-root", str(code), ruling="fixed in commit deadbee")
        self.assertEqual(rc, 1)
        self.assertIn("does not resolve", err)
        self.assertEqual(sha(self.topic), before)
        rc, _o, err = self.append("--code-root", str(code), "--evidence", "commit deadbee")
        self.assertEqual(rc, 1, err)
        rc, _o, err = self.append("--code-root", str(code), ruling=f"fixed in commit {good}")
        self.assertEqual(rc, 0, err)
        # without a code root the linter's own warning stands: no refusal here
        rc, _o, err = self.append(ruling="see commit deadbee")
        self.assertEqual(rc, 0, err)


class TestDerivedAuthority(FilingCase):
    def test_verbatim_quote_is_derived_exactly(self):
        self.write_transcript()
        rc, _o, err = self.append("--session", SID, "--owner-message", OWNER_UUID,
                                  "--quote", "make manual wiring the only official way  and document")
        self.assertEqual(rc, 0, err)
        link = self.parsed_links(self.topic.read_text())[0]
        self.assertEqual(link["ruling"]["authority"], "owner-verbatim")
        self.assertEqual(link["ruling"]["text"], "make manual wiring the only official way  and document")
        self.assertIn(f"owner message {OWNER_UUID}, session {SID}, 2026-11-07T20:01:00.000Z", link["ruling"]["source"])
        # the surrounding message is written nowhere, hook.log included
        self.assertNotIn("I told you already", self.topic.read_text())
        self.assertNotIn("manual wiring", (self.home / "hook.log").read_text())
        self.assertEqual(run_memlint(["--against-ref", "HEAD", str(self.store)])[0], 0)

    def test_collapsed_space_quote_falls_back_to_inference_with_warning(self):
        self.write_transcript()
        rc, _o, err = self.append("--session", SID, "--owner-message", OWNER_UUID,
                                  "--quote", "the only official way and document")  # one space, message has two
        self.assertEqual(rc, 0, err)
        self.assertIn("WARNING", err)
        link = self.parsed_links(self.topic.read_text())[0]
        self.assertEqual(link["ruling"]["authority"], "agent-inference")
        self.assertIn("transcript unavailable: the quote is not an exact substring", link["ruling"]["source"])
        self.assertIn("derived=unavailable", self.log_lines()[-1])

    def test_missing_transcript_falls_back_to_inference(self):
        rc, _o, err = self.append("--session", "no-such-session", "--yes-message", YES_UUID)
        self.assertEqual(rc, 0, err)
        self.assertIn("WARNING", err)
        self.assertEqual(self.parsed_links(self.topic.read_text())[0]["ruling"]["authority"], "agent-inference")

    def test_tool_result_and_meta_uuids_are_not_human(self):
        self.write_transcript()
        for uid in (TOOL_UUID, META_UUID):
            with self.subTest(uid=uid):
                before = self.topic.read_text()
                rc, _o, err = self.append("--session", SID, "--yes-message", uid)
                self.assertEqual(rc, 0, err)
                self.assertIn("not a human message", err)
                self.assertEqual(self.parsed_links(self.topic.read_text())[0]["ruling"]["authority"],
                                 "agent-inference")
                self.topic.write_text(before)
        rc, _o, err = self.append("--session", SID, "--yes-message", "ffffffff-0000-0000-0000-000000000000")
        self.assertIn("not in the session transcript", err)

    def test_two_transcripts_are_ambiguous_without_a_code_root_preference(self):
        self.write_transcript("proj", cwd="/work/p")
        self.write_transcript("other", cwd="/elsewhere/q")
        before = sha(self.topic)
        rc, _o, err = self.append("--session", SID, "--yes-message", YES_UUID)
        self.assertEqual(rc, 1)
        self.assertIn("transcripts match session", err)
        self.assertEqual(sha(self.topic), before)
        work = self.tmp / "work"
        (work / "p").mkdir(parents=True)
        self.write_transcript("proj", cwd=str(work / "p"))
        rc, _o, err = self.append("--session", SID, "--yes-message", YES_UUID, "--code-root", str(work / "p"))
        self.assertEqual(rc, 0, err)

    def test_yes_message_is_ratified_with_the_shown_text(self):
        self.write_transcript()
        rc, _o, err = self.append("--session", SID, "--yes-message", YES_UUID, "--shown-text", "the exact text shown")
        self.assertEqual(rc, 0, err)
        link = self.parsed_links(self.topic.read_text())[0]
        self.assertEqual(link["ruling"]["authority"], "owner-ratified")
        self.assertEqual(link["ruling"]["text"], "the exact text shown")
        self.assertIn(f"owner yes-message {YES_UUID}", link["ruling"]["source"])
        # default shown text is --ruling
        rc, _o, err = self.append("--session", SID, "--yes-message", YES_UUID, ruling="ruling is the shown text")
        self.assertEqual(self.parsed_links(self.topic.read_text())[0]["ruling"]["text"], "ruling is the shown text")

    def test_question_form_verbatim_is_refused_by_the_linter(self):
        self.write_transcript()
        before = sha(self.topic)
        rc, _o, err = self.append("--session", SID, "--owner-message", OWNER_UUID, "--quote", "official way  and document")
        self.assertEqual(rc, 0, err)  # control: not a question
        self.topic.write_text(TOPIC)
        extra = [{"type": "user", "uuid": "bbbbbbbb-0000-0000-0000-000000000009", "sessionId": SID, "cwd": "/work/p",
                  "timestamp": "2026-11-07T20:05:00.000Z", "message": {"role": "user", "content": "is it done yet?"}}]
        self.write_transcript(extra=extra)
        rc, _o, err = self.append("--session", SID, "--owner-message", "bbbbbbbb-0000-0000-0000-000000000009",
                                  "--quote", "is it done yet?")
        self.assertEqual(rc, 1)
        self.assertIn("question", err)
        self.assertEqual(sha(self.topic), before)


class TestPromote(FilingCase):
    def test_promote_changes_the_old_link_by_exactly_one_line(self):
        self.write_transcript()
        old = self.prov.read_text()
        rc, out, err = self.run_cmd("promote", "TOP-9101", "L1", "--session", SID, "--yes-message", YES_UUID)
        self.assertEqual(rc, 0, err)
        new = self.prov.read_text()
        old_span, new_span = self.spans(old)["L1"], self.spans(new)["L1"]
        added = [l for l in new_span.splitlines(keepends=True) if l not in old_span.splitlines(keepends=True)]
        self.assertEqual(added, ["    promoted_by: L2\n"])
        self.assertEqual(len(new_span.splitlines()), len(old_span.splitlines()) + 1)
        old_l1, new_l1 = self.parsed_links(old)[0], [l for l in self.parsed_links(new) if l["link"] == "L1"][0]
        self.assertEqual({k: v for k, v in new_l1.items() if k != "promoted_by"}, old_l1)
        l2 = self.parsed_links(new)[0]
        self.assertEqual(l2["link"], "L2")
        self.assertEqual(l2["ruling"]["authority"], "owner-ratified")
        self.assertEqual(l2["ruling"]["text"], "an inference awaiting the owner")  # default: the text the owner saw
        self.assertEqual(l2["rationale"], {"text": "the reasoning", "authority": "agent-inference"})
        self.assertEqual(l2["status"], "active")
        self.assertEqual(run_memlint(["--against-ref", "HEAD", str(self.store)])[0], 0)
        # a second promote of the same link is refused
        before = sha(self.prov)
        rc, _o, err = self.run_cmd("promote", "TOP-9101", "L1", "--session", SID, "--yes-message", YES_UUID)
        self.assertEqual(rc, 1)
        self.assertIn("already carries promoted_by", err)
        self.assertEqual(sha(self.prov), before)

    def test_promote_verbatim_uses_the_quote(self):
        self.write_transcript()
        rc, _o, err = self.run_cmd("promote", "TOP-9101", "L1", "--session", SID, "--owner-message", OWNER_UUID,
                                   "--quote", "manual wiring the only official way")
        self.assertEqual(rc, 0, err)
        l2 = self.parsed_links(self.prov.read_text())[0]
        self.assertEqual((l2["ruling"]["authority"], l2["ruling"]["text"]),
                         ("owner-verbatim", "manual wiring the only official way"))

    def test_promote_with_unreachable_transcript_is_refused(self):
        before = sha(self.prov)
        rc, _o, err = self.run_cmd("promote", "TOP-9101", "L1", "--session", "nope", "--yes-message", YES_UUID)
        self.assertEqual(rc, 1)
        self.assertIn("cannot derive an owner tier", err)
        self.assertEqual(sha(self.prov), before)
        self.write_transcript()
        rc, _o, err = self.run_cmd("promote", "TOP-9101", "L1", "--session", SID, "--yes-message", TOOL_UUID)
        self.assertEqual(rc, 1)
        self.assertEqual(sha(self.prov), before)


class TestRecord(FilingCase):
    def test_id_allocation_max_plus_one_across_topics_and_inbox(self):
        inbox = self.store / "inbox"
        inbox.mkdir()
        (inbox / "drop.md").write_text("---\nid: TOP-9500\n---\n\nnote\n")
        rc, out, err = self.run_cmd("record", "--title", "Fresh topic: with colon", "--area", "memory",
                                    "--ruling", "decided", "--tags", "a,b-c,yes", "--question", "What now?")
        self.assertEqual(rc, 0, err)
        path = self.store / "topics" / "memory" / "fresh-topic-with-colon.md"
        text = path.read_text()
        fm = memidx.parse_record_raw(text, path).frontmatter
        self.assertEqual(fm["id"], "TOP-9501")
        self.assertEqual(fm["title"], "Fresh topic: with colon")
        self.assertEqual(fm["tags"], ["a", "b-c", "yes"])
        self.assertEqual(fm["current"], "L1")
        self.assertEqual(text.split("---\n", 2)[2].strip(), "What now?")
        errors, _w = memlint.lint_root(self.store)
        self.assertEqual(errors, [])

    def test_body_defaults_to_the_title_and_paths_collide_loudly(self):
        rc, _o, err = self.run_cmd("record", "--title", "Plain title", "--area", "memory", "--ruling", "r")
        self.assertEqual(rc, 0, err)
        text = (self.store / "topics" / "memory" / "plain-title.md").read_text()
        self.assertEqual(text.split("---\n", 2)[2].strip(), "Plain title")
        before = sha(self.store / "topics" / "memory" / "plain-title.md")
        rc, _o, err = self.run_cmd("record", "--title", "Plain title", "--area", "memory", "--ruling", "r")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)
        rc, _o, err = self.run_cmd("record", "--title", "Other", "--area", "memory", "--ruling", "r", "--id", "TOP-9100")
        self.assertEqual(rc, 1)
        self.assertIn("already carried", err)
        self.assertEqual(sha(self.store / "topics" / "memory" / "plain-title.md"), before)

    def test_provisional_record_has_no_current(self):
        rc, _o, err = self.run_cmd("record", "--title", "Tentative idea", "--area", "memory", "--ruling", "r",
                                   "--status", "provisional")
        self.assertEqual(rc, 0, err)
        path = self.store / "topics" / "memory" / "tentative-idea.md"
        fm = memidx.parse_record_raw(path.read_text(), path).frontmatter
        self.assertNotIn("current", fm)
        errors, _w = memlint.lint_root(self.store)
        self.assertEqual(errors, [])


class TestDedupeRetrievalStanding(FilingCase):
    def reindex(self):
        rc, _o, err = run_memidx(["reindex", "--root", str(self.store), "--project", "p",
                                  "--db", str(self.db), "--no-embed"])
        self.assertEqual(rc, 0, err)

    def test_dedupe_lists_three_hits_and_record_still_writes(self):
        for n in range(4):
            (self.store / "topics" / "memory" / f"gadget{n}.md").write_text(
                PROVISIONAL_TOPIC.replace("TOP-9101", f"TOP-920{n}").replace(
                    "Provisional fixture", "Gadget cache invalidation policy"))
        self.reindex()
        rc, out, err = self.run_cmd("record", "--title", "Gadget cache invalidation policy", "--area", "memory",
                                    "--ruling", "r")
        self.assertEqual(rc, 0, err)
        hit_lines = [l for l in out.splitlines() if l.startswith("  TOP-")]
        self.assertEqual(len(hit_lines), 3, out)
        self.assertIn("suggestion: append TOP-", out)
        self.assertTrue((self.store / "topics" / "memory" / "gadget-cache-invalidation-policy.md").exists())

    def test_retrieval_miss_warns_and_require_retrieve_gives_rc3(self):
        """The miss is real: `filing_search_hits` runs unmocked and the post-write
        title search (the `search` argv without --hydrate) comes back empty
        through `main`."""
        real_main = memidx.main

        def main_with_empty_title_search(argv=None):
            if argv and argv[0] == "search" and "--hydrate" not in argv:
                print("[]")
                return 0
            return real_main(argv)

        with mock.patch.object(memidx, "main", main_with_empty_title_search):
            rc, _o, err = self.append()
            self.assertEqual(rc, 0)
            self.assertIn("WARNING: retrieval check missed (title search did not return TOP-9100)", err)
            self.assertIn("retrieval=miss", self.log_lines()[-1])
            rc, _o, err = self.append("--require-retrieve")
            self.assertEqual(rc, 3)
        # the write is in place either way
        self.assertEqual(len(self.parsed_links(self.topic.read_text())), 4)

    def test_retrieval_hit_after_a_real_reindex(self):
        rc, _o, err = self.append()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("retrieval check missed", err)
        self.assertIn("retrieval=hit", self.log_lines()[-1])

    def test_standing_suggestion_only_for_an_owner_tier_active_link(self):
        rc, out, err = self.append()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("mark-standing", out)
        self.write_transcript()
        rc, out, err = self.append("--session", SID, "--yes-message", YES_UUID)
        self.assertEqual(rc, 0, err)
        self.assertIn("memidx.py mark-standing TOP-9100 L4 --add --authority owner-ratified", out)
        self.assertNotIn("standing:", self.topic.read_text())  # never written by the filing command

    def test_session_prints_the_read_only_proposal_section(self):
        sessions = self.home / "sessions" / "p"
        sessions.mkdir(parents=True)
        (sessions / f"{SID}.json").write_text(json.dumps({"ledger": [], "search_fallbacks": []}))
        before_bindings = self.topic.read_text().count("code_refs")
        rc, out, err = self.append("--session", SID)
        self.assertEqual(rc, 0, err)
        self.assertIn("Binding proposals", out)
        self.assertEqual(self.topic.read_text().count("code_refs"), before_bindings)


class TestLogReceipt(FilingCase):
    def test_receipt_fields_without_text(self):
        rc, _o, err = self.append(ruling="SECRET ruling words")
        self.assertEqual(rc, 0, err)
        line = self.log_lines()[-1]
        for field in ("outcome=written", "topic=TOP-9100", "link=L3", "authority=agent-inference",
                      "derived=no", "dedupe_top=", "retrieval=hit", "project=p"):
            self.assertIn(field, line)
        self.assertNotIn("SECRET", (self.home / "hook.log").read_text())
        rc, _o, _e = self.append("--dry-run")
        self.assertIn("outcome=dry-run", self.log_lines()[-1])
        rc, _o, _e = self.run_cmd("append", "TOP-0000", "--ruling", "x")
        self.assertEqual(rc, 1)
        self.assertIn("outcome=refused", self.log_lines()[-1])
        self.assertIn("reason=unknown-topic", self.log_lines()[-1])
        self.assertTrue(self.log_lines()[-1].rstrip().endswith("project=p") or "project=p" in self.log_lines()[-1])


class TestGateRound1(FilingCase):
    """Gate round 1 fixes (R1-1..R1-8)."""

    def test_id_with_trailing_comment_is_seen_by_the_id_collector(self):
        """R1-1: `id: TOP-9500 # comment` is valid YAML the old regex missed."""
        inbox = self.store / "inbox"
        inbox.mkdir()
        (inbox / "drop.md").write_text("---\nid: TOP-9500 # a comment\n---\n\nnote\n")
        rc, _o, err = self.run_cmd("record", "--title", "Collides", "--area", "memory",
                                   "--ruling", "r", "--id", "TOP-9500")
        self.assertEqual(rc, 1, err)
        self.assertIn("already carried", err)
        self.assertFalse((self.store / "topics" / "memory" / "collides.md").exists())
        rc, _o, err = self.run_cmd("record", "--title", "Allocated", "--area", "memory", "--ruling", "r")
        self.assertEqual(rc, 0, err)
        fm = memidx.parse_record_raw((self.store / "topics" / "memory" / "allocated.md").read_text(), "x").frontmatter
        self.assertEqual(fm["id"], "TOP-9501")

    # --- R1-2: atomic writes, I/O errors are refusals --------------------------------

    def test_io_error_on_append_is_a_refusal_and_leaves_the_file_alone(self):
        before = sha(self.topic)
        listing = sorted(os.listdir(self.topic.parent))
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            rc, _o, err = self.append()
        self.assertEqual(rc, 1, err)
        self.assertIn("cannot write", err)
        self.assertEqual(sha(self.topic), before)
        self.assertEqual(sorted(os.listdir(self.topic.parent)), listing)  # no temp file left
        self.assertIn("reason=io-error", self.log_lines()[-1])

    def test_io_error_on_record_leaves_no_file_and_no_temp(self):
        directory = self.store / "topics" / "memory"
        listing = sorted(os.listdir(directory))
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            rc, _o, err = self.run_cmd("record", "--title", "Doomed", "--area", "memory", "--ruling", "r")
        self.assertEqual(rc, 1, err)
        self.assertIn("cannot write", err)
        self.assertEqual(sorted(os.listdir(directory)), listing)
        self.assertIn("reason=io-error", self.log_lines()[-1])

    # --- R1-3: the guard is a proof, or the filing is refused ------------------------

    def test_guard_error_undoes_an_append_a_promote_and_a_record(self):
        for argv, rel in ((["append", "TOP-9100", "--ruling", "x"], "topics/memory/widget.md"),):
            before = sha(self.topic)
            with self._guard_error(rel):
                rc, _o, err = self.run_cmd(*argv)
            self.assertEqual(rc, 1, err)
            self.assertIn("append-only guard failed", err)
            self.assertEqual(sha(self.topic), before)
        self.write_transcript()
        before = sha(self.prov)
        with self._guard_error("topics/memory/prov.md"):
            rc, _o, err = self.run_cmd("promote", "TOP-9101", "L1", "--session", SID, "--yes-message", YES_UUID)
        self.assertEqual(rc, 1, err)
        self.assertEqual(sha(self.prov), before)
        new = self.store / "topics" / "memory" / "guarded.md"
        with self._guard_error("topics/memory/guarded.md"):
            rc, _o, err = self.run_cmd("record", "--title", "Guarded", "--area", "memory", "--ruling", "r")
        self.assertEqual(rc, 1, err)
        self.assertFalse(new.exists())
        self.assertIn("reason=guard", self.log_lines()[-1])

    def test_guard_that_cannot_run_is_a_refusal_not_a_pass(self):
        before = sha(self.topic)
        with mock.patch.object(memlint, "check_append_only", side_effect=memlint.GitError("no HEAD")):
            rc, _o, err = self.append()
        self.assertEqual(rc, 1, err)
        self.assertIn("cannot run", err)
        self.assertEqual(sha(self.topic), before)
        self.assertIn("reason=guard-unavailable", self.log_lines()[-1])
        with mock.patch.object(memlint, "check_append_only", side_effect=memlint.GitError("no HEAD")):
            rc, _o, err = self.run_cmd("record", "--title", "Unproven", "--area", "memory", "--ruling", "r")
        self.assertEqual(rc, 1, err)
        self.assertFalse((self.store / "topics" / "memory" / "unproven.md").exists())

    def test_guard_does_not_see_an_untracked_new_topic(self):
        """Verified finding: `check_append_only` reads `git diff HEAD`, so an
        untracked brand-new topic yields no error even when it is malformed --
        the new-topic proof is the lint + new-link checks, not the guard."""
        (self.store / "topics" / "memory" / "untracked.md").write_text("---\ntype: topic\nid: TOP-9300\n---\n")
        errors, changed, _notes = memlint.check_append_only(self.store, "HEAD", False)
        self.assertEqual((errors, changed), ([], 0))

    # --- R1-4: no error-set subtraction ----------------------------------------------

    def test_a_topic_that_already_fails_lint_accepts_no_filing(self):
        bad = self.store / "topics" / "memory" / "bad.md"
        bad.write_text(PROVISIONAL_TOPIC.replace("TOP-9101", "TOP-9110").replace(
            "status: provisional", "status: bogus"))
        self.commit("bad topic")
        self.head0 = self.head()
        before = sha(bad)
        rc, _o, err = self.run_cmd("append", "TOP-9110", "--ruling", "x")
        self.assertEqual(rc, 1, err)
        self.assertIn("unknown status", err)
        self.assertEqual(sha(bad), before)

    def test_an_old_link_that_only_warns_under_a_grandfathered_check_still_accepts_a_filing(self):
        old = self.store / "topics" / "memory" / "oldkind.md"
        old.write_text(PROVISIONAL_TOPIC.replace("TOP-9101", "TOP-9111").replace("kind: adopted", "kind: weird"))
        self.commit("old kind")
        self.head0 = self.head()
        self.assertEqual(memlint.lint_root(self.store)[0], [])  # control: a warning in plain mode
        rc, _o, err = self.run_cmd("append", "TOP-9111", "--ruling", "x")
        self.assertEqual(rc, 0, err)

    # --- R1-5: the tie-break reads the FIRST human line's cwd -------------------------

    def test_first_human_line_without_cwd_cannot_win_the_tie_break(self):
        work = self.tmp / "work"
        (work / "p").mkdir(parents=True)
        for name, cwd in (("proj", "/elsewhere/q"), ("other", "/elsewhere/r")):
            self.write_transcript(name, cwd=cwd)
        # in "proj" the first human line has no cwd; a later one lies under the code root
        lines = [
            {"type": "user", "uuid": "cccccccc-0000-0000-0000-000000000001", "sessionId": SID,
             "timestamp": "2026-11-07T20:00:00.000Z", "message": {"role": "user", "content": "first"}},
            {"type": "user", "uuid": YES_UUID, "sessionId": SID, "cwd": str(work / "p"),
             "timestamp": "2026-11-07T20:03:00.000Z", "message": {"role": "user", "content": "ok"}},
        ]
        (self.transcripts / "proj" / f"{SID}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        before = sha(self.topic)
        rc, _o, err = self.append("--session", SID, "--yes-message", YES_UUID, "--code-root", str(work / "p"))
        self.assertEqual(rc, 1, err)
        self.assertIn("transcripts match session", err)
        self.assertIn("reason=ambiguous-transcript", self.log_lines()[-1])
        self.assertEqual(sha(self.topic), before)

    # --- R1-8: refusals and behaviours the first round left untested ------------------

    def _refused_unchanged(self, path, argv, needle=None):
        before = sha(path)
        rc, _o, err = self.run_cmd(*argv)
        self.assertEqual(rc, 1, err)
        if needle:
            self.assertIn(needle, err)
        self.assertEqual(sha(path), before)
        return err

    def test_malformed_frontmatter_is_refused(self):
        bad = self.store / "topics" / "memory" / "broken.md"
        bad.write_text("---\ntype: topic\nid: TOP-9120\ntitle: Broken\nlinks:\n  - link: L1\n")
        self._refused_unchanged(bad, ["append", "TOP-9120", "--ruling", "x"], "does not parse")

    def test_missing_links_key_is_refused(self):
        nolinks = self.store / "topics" / "memory" / "nolinks.md"
        nolinks.write_text("---\ntype: topic\nid: TOP-9121\ntitle: No links\narea: memory\n---\n\nB\n")
        self._refused_unchanged(nolinks, ["append", "TOP-9121", "--ruling", "x"], "links")

    def test_two_links_with_the_same_id_are_refused(self):
        dup = self.store / "topics" / "memory" / "duplink.md"
        dup.write_text(TOPIC.replace("TOP-9100", "TOP-9122").replace(
            "  - link: L1\n", "  - link: L2\n", 1).replace("    reverses: L1\n", "", 1)
            .replace("    reason_for_change: changed-context\n", "", 1))
        self.assertEqual(dup.read_text().count("link: L2"), 2)
        self._refused_unchanged(dup, ["append", "TOP-9122", "--ruling", "x"], "duplicate link id")

    def test_forced_roundtrip_failure_is_refused(self):
        before = sha(self.topic)
        with mock.patch.object(memidx, "edit_roundtrip_problem", return_value="forced mismatch"):
            rc, _o, err = self.append()
        self.assertEqual(rc, 1, err)
        self.assertIn("forced mismatch", err)
        self.assertEqual(sha(self.topic), before)
        self.assertIn("reason=roundtrip", self.log_lines()[-1])

    def test_declined_newest_link_leaves_current_unchanged(self):
        rc, _o, err = self.append("--status", "declined", "--kind", "declined")
        self.assertEqual(rc, 0, err)
        text = self.topic.read_text()
        self.assertIn("current: L2", text)
        self.assertEqual(self.parsed_links(text)[0]["status"], "declined")
        self.assertEqual(run_memlint(["--against-ref", "HEAD", str(self.store)])[0], 0)

    def test_provisional_owner_tier_link_prints_no_standing_suggestion(self):
        self.write_transcript()
        rc, out, err = self.append("--session", SID, "--yes-message", YES_UUID, "--status", "provisional")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.parsed_links(self.topic.read_text())[0]["ruling"]["authority"], "owner-ratified")
        self.assertNotIn("mark-standing", out)

    def test_exactly_one_receipt_line_per_run(self):
        counts = [len(self.log_lines())]
        for argv in (("append", "TOP-9100", "--ruling", "ok"),               # written
                     ("append", "TOP-9100", "--ruling", "dry", "--dry-run"),  # dry-run
                     ("append", "TOP-0000", "--ruling", "x")):               # refused
            self.run_cmd(*argv)
            counts.append(len(self.log_lines()))
        self.assertEqual([b - a for a, b in zip(counts, counts[1:])], [1, 1, 1])
        self.assertEqual([l.split("outcome=")[1].split()[0] for l in self.log_lines()],
                         ["written", "dry-run", "refused"])


class TestGateRound2(FilingCase):
    """Gate round 2 (TOP-0137 L23)."""

    # --- R2-1: a failed rollback is reported as such ---------------------------------

    def test_failed_undo_of_an_append_reports_the_rejected_edit_on_disk(self):
        before = sha(self.topic)
        real = memidx._atomic_write_bytes
        written = []

        def first_ok_then_fail(path, data, exclusive=False):
            if written:
                raise OSError("disk gone")
            written.append(data)
            real(path, data, exclusive)

        with self._guard_error("topics/memory/widget.md"), \
                mock.patch.object(memidx, "_atomic_write_bytes", side_effect=first_ok_then_fail):
            rc, _o, err = self.append()
        self.assertEqual(rc, 1, err)
        self.assertEqual(len(written), 1)
        self.assertNotEqual(sha(self.topic), before)
        self.assertEqual(self.topic.read_bytes(), written[0])  # the whole rejected text
        self.assertIn("REJECTED EDIT REMAINS ON DISK", err)
        self.assertNotIn("nothing written", err)
        self.assertIn(str(self.topic), err)
        self.assertIn(f"git -C {self.store.resolve()} checkout -- topics/memory/widget.md", err)
        self.assertIn("reason=guard rollback=failed", self.log_lines()[-1])

    def test_failed_unlink_of_a_new_topic_names_rm(self):
        new = self.store / "topics" / "memory" / "stuck.md"
        with self._guard_error("topics/memory/stuck.md"), \
                mock.patch.object(Path, "unlink", side_effect=OSError("busy")):
            rc, _o, err = self.run_cmd("record", "--title", "Stuck", "--area", "memory", "--ruling", "r")
        self.assertEqual(rc, 1, err)
        self.assertTrue(new.exists())
        self.assertIn("REJECTED EDIT REMAINS ON DISK", err)
        self.assertIn(f"rm {new.resolve()}", err)
        self.assertIn("reason=guard rollback=failed", self.log_lines()[-1])

    def test_a_successful_undo_has_no_rollback_field(self):
        with self._guard_error("topics/memory/widget.md"):
            rc, _o, err = self.append()
        self.assertEqual(rc, 1, err)
        self.assertIn("nothing written", err)
        self.assertNotIn("rollback", self.log_lines()[-1])


if __name__ == "__main__":
    unittest.main()
