"""Tests for SCHEMA v1.1 addendum features: edges, assumptions, invariants,
concepts, `why`, and `drift`.

Owned by this session (per the concurrent-work split): memidx.py additions,
memlint.py additions, this file, and fixtures/v11/. Does not touch hooks/,
skills/, or tests/test_hooks.py.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

FIXTURES = TOOLS_DIR / "fixtures" / "v11"
HIDDEN_FILES_FIXTURE = (
    TOOLS_DIR / "fixtures" / "schema" / "topics" / "processing" / "hidden-files-in-count.md"
)
MEMLINT_CLEAN = TOOLS_DIR / "fixtures" / "memlint_clean"


def ns(**kw):
    base = dict(project=memidx.DEFAULT_PROJECT, db=None)
    base.update(kw)
    return SimpleNamespace(**base)


@contextlib.contextmanager
def mc_home(path):
    """Pins MEMCONTINUUM_HOME to `path` for the duration of the block --
    finding 7's code-index fast path (resolve_symbol_to_path, when a
    project is given) reads this env var directly, and must never be
    exercised against a real ~/.memcontinuum on the machine running the
    test."""
    old = os.environ.get("MEMCONTINUUM_HOME")
    os.environ["MEMCONTINUUM_HOME"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("MEMCONTINUUM_HOME", None)
        else:
            os.environ["MEMCONTINUUM_HOME"] = old


def reindex(root, db, project=memidx.DEFAULT_PROJECT, full=False, no_embed=True):
    args = ns(root=str(root), db=str(db), project=project, full=full, no_embed=no_embed)
    return memidx.cmd_reindex(args)


def build_root(td) -> Path:
    """A markdown root with the v1.1 topic, the concept, and (for the
    declined-link 'why' case) the existing hidden-files topic chain."""
    root = Path(td) / "root"
    (root / "topics" / "deletion").mkdir(parents=True)
    shutil.copy(
        FIXTURES / "topics" / "deletion" / "delete-gate-invariant.md",
        root / "topics" / "deletion" / "delete-gate-invariant.md",
    )
    (root / "concepts").mkdir()
    shutil.copy(FIXTURES / "concepts" / "media-identity.md", root / "concepts" / "media-identity.md")
    (root / "topics" / "processing").mkdir(parents=True)
    shutil.copy(
        HIDDEN_FILES_FIXTURE, root / "topics" / "processing" / HIDDEN_FILES_FIXTURE.name
    )
    return root


def run_capturing(func, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = func(args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 1. edges + assumptions
# ---------------------------------------------------------------------------


class TestEdgesTable(unittest.TestCase):
    def test_edges_table_populated_from_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            conn = memidx.open_db(db)
            rows = conn.execute(
                "SELECT * FROM edges WHERE project=? ORDER BY rel",
                (memidx.DEFAULT_PROJECT,),
            ).fetchall()
            conn.close()
            self.assertEqual(len(rows), 3, rows)
            rels = {r["rel"] for r in rows}
            self.assertEqual(rels, {"supersedes", "challenged_by", "abandons"})
            supersedes = next(r for r in rows if r["rel"] == "supersedes")
            self.assertEqual(supersedes["from_ref"], "TOP-0100/L2")
            self.assertEqual(supersedes["to_ref"], "TOP-0100/L1")


class TestAssumptionsTable(unittest.TestCase):
    def test_assumptions_table_populated_with_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            conn = memidx.open_db(db)
            rows = conn.execute(
                "SELECT * FROM assumptions WHERE project=? ORDER BY aid",
                (memidx.DEFAULT_PROJECT,),
            ).fetchall()
            conn.close()
            self.assertEqual(len(rows), 2, rows)
            a1 = next(r for r in rows if r["aid"] == "A1")
            self.assertEqual(a1["status"], "broken")
            self.assertEqual(a1["since"], "2026-08-29")
            self.assertEqual(a1["topic_id"], "TOP-0100")
            self.assertEqual(a1["link"], "L2")
            a2 = next(r for r in rows if r["aid"] == "A2")
            self.assertEqual(a2["status"], "holds")


class TestChainShowsEdgesAndAssumptions(unittest.TestCase):
    def test_chain_text_shows_edge_lines_and_broken_assumptions(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            args = ns(db=str(db), project=memidx.DEFAULT_PROJECT, topic="TOP-0100", json=False)
            rc, out = run_capturing(memidx.cmd_chain, args)
            self.assertEqual(rc, 0)
            self.assertIn("↳ supersedes → TOP-0100/L1", out)
            self.assertIn("↳ challenged_by → INC-900", out)
            self.assertIn("↳ abandons → assumption:A1", out)
            self.assertIn("A1", out)
            self.assertIn("broken", out.lower())

    def test_chain_json_includes_edges_and_broken_assumptions(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            args = ns(db=str(db), project=memidx.DEFAULT_PROJECT, topic="TOP-0100", json=True)
            rc, out = run_capturing(memidx.cmd_chain, args)
            self.assertEqual(rc, 0)
            data = json.loads(out)
            l2 = next(l for l in data["links"] if l["link"] == "L2")
            self.assertEqual(len(l2["edges"]), 3)
            self.assertEqual(len(l2["assumptions"]), 2)
            self.assertIn("broken_assumptions", data)
            self.assertEqual([a["aid"] for a in data["broken_assumptions"]], ["A1"])


class TestEdgeRelValidation(unittest.TestCase):
    def test_unknown_rel_is_error(self):
        errors, _warnings = memlint.lint_root(FIXTURES / "memlint_bad")
        self.assertTrue(any("obsoletes" in e and "rel" in e for e in errors), errors)

    def test_known_rels_are_not_flagged(self):
        errors, _warnings = memlint.lint_root(FIXTURES / "topics")
        self.assertFalse(any("rel" in e for e in errors), errors)


# ---------------------------------------------------------------------------
# 2. concepts
# ---------------------------------------------------------------------------


class TestConceptIndexing(unittest.TestCase):
    def test_concepts_and_concept_paths_tables_populated(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            conn = memidx.open_db(db)
            crow = conn.execute(
                "SELECT * FROM concepts WHERE project=? AND id=?",
                (memidx.DEFAULT_PROJECT, "CON-007"),
            ).fetchone()
            self.assertIsNotNone(crow)
            self.assertEqual(crow["title"], "Delete Gate")
            paths = conn.execute(
                "SELECT * FROM concept_paths WHERE project=? AND concept_id=?",
                (memidx.DEFAULT_PROJECT, "CON-007"),
            ).fetchall()
            conn.close()
            kinds = {p["kind"] for p in paths}
            self.assertEqual(kinds, {"implemented_by", "tested_by"})


class TestForPathConcepts(unittest.TestCase):
    def test_for_path_returns_concept_then_governed_topic_chains_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                file_path="Sources/Delete/DeleteGate.swift", json=True,
            )
            rc, out = run_capturing(memidx.cmd_for_path, args)
            self.assertEqual(rc, 0)
            data = json.loads(out)
            con = next(e for e in data if e.get("id") == "CON-007")
            governed_ids = {g["id"] for g in con["governed_by"]}
            self.assertEqual(governed_ids, {"TOP-0100", "TOP-0042"})

    def test_for_path_text_mode_shows_concept_only_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            # Tests/DeleteGateTests.swift is only in CON-007.tested_by, not in
            # any topic's code_refs -- a concept-only match.
            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                file_path="Tests/DeleteGateTests.swift", json=False,
            )
            rc, out = run_capturing(memidx.cmd_for_path, args)
            self.assertEqual(rc, 0)
            self.assertNotIn("no topics reference this path", out)
            self.assertIn("CON-007", out)
            self.assertIn("TOP-0100", out)


class TestConceptMemlint(unittest.TestCase):
    def test_missing_path_is_error_when_code_root_given(self):
        errors, _warnings = memlint.lint_root(FIXTURES / "concepts", code_root=FIXTURES / "code")
        self.assertTrue(
            any("CON-900" in e and "DoesNotExist.swift" in e for e in errors), errors
        )

    def test_missing_path_not_checked_without_code_root(self):
        errors, _warnings = memlint.lint_root(FIXTURES / "concepts")
        self.assertFalse(any("DoesNotExist.swift" in e for e in errors), errors)

    def test_no_tested_by_is_warning_not_error(self):
        errors, warnings = memlint.lint_root(FIXTURES / "concepts", code_root=FIXTURES / "code")
        self.assertFalse(any("CON-901" in e for e in errors), errors)
        self.assertTrue(any("CON-901" in w and "tested_by" in w for w in warnings), warnings)

    def test_clean_concept_has_no_missing_path_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(FIXTURES / "concepts" / "media-identity.md", root / "media-identity.md")
            errors, _warnings = memlint.lint_root(root, code_root=FIXTURES / "code")
            self.assertEqual(errors, [], errors)

    def test_hash_fragment_stripped_before_existence_check(self):
        # implemented_by entries carry "#symbol" -- must check only the file.
        errors, _warnings = memlint.lint_root(FIXTURES / "concepts", code_root=FIXTURES / "code")
        self.assertFalse(any("DeleteGate.swift#delete" in e for e in errors), errors)


class TestMemlintCLIBackwardCompat(unittest.TestCase):
    def test_bare_root_still_works(self):
        rc = memlint.main([str(MEMLINT_CLEAN)])
        self.assertEqual(rc, 0)

    def test_code_root_flag_enables_concept_path_check(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(FIXTURES / "concepts" / "bad-missing-path.md", root / "bad.md")
            rc = memlint.main([str(root), "--code-root", str(FIXTURES / "code")])
            self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# 3. why
# ---------------------------------------------------------------------------


class TestWhy(unittest.TestCase):
    def test_why_by_path_includes_declined_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                symbol_or_path="Sources/Delete/DeleteGate.swift", code_root=None, json=False,
            )
            rc, out = run_capturing(memidx.cmd_why, args)
            self.assertEqual(rc, 0)
            self.assertIn("TOP-0042", out)
            self.assertIn("declined", out)
            self.assertIn("counting hidden files in the main total was rejected", out)

    def test_why_by_bare_symbol_resolves_via_code_root_grep(self):
        # Finding 7: cmd_why now passes project=args.project into
        # resolve_symbol_to_path, which (when a project is given) tries
        # the CODE INDEX fast path first, reading MEMCONTINUUM_HOME --
        # pin it to a fresh temp dir so this test never touches a real
        # ~/.memcontinuum/default-code.sqlite on the machine it runs on
        # (it has none here anyway, but must not depend on that).
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            with mc_home(Path(td) / "home"):
                args = ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT,
                    symbol_or_path="DeleteGate", code_root=str(FIXTURES / "code"), json=False,
                )
                rc, out = run_capturing(memidx.cmd_why, args)
            self.assertEqual(rc, 0)
            self.assertIn("CON-007", out)
            self.assertIn("TOP-0100", out)

    def test_why_bare_symbol_without_code_root_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                symbol_or_path="DeleteGate", code_root=None, json=False,
            )
            rc = memidx.cmd_why(args)
            self.assertEqual(rc, 2)

    def test_bare_symbol_resolution_finds_a_backtick_name_the_old_regex_missed(self):
        """Finding 7: `why`'s bare-symbol resolver must consume the SAME
        chunker lexer chunk_source/declared_symbol_names/memlint/
        code-search attachment all agree on, not its own from-scratch
        regex (which only knew func/class/struct/enum/let/var -- no init,
        subscript, operators, or backtick-quoted names, e.g. `` `default` ``)."""
        with tempfile.TemporaryDirectory() as td:
            code_root = Path(td) / "code"
            code_root.mkdir()
            (code_root / "Escaped.swift").write_text(
                "class Escaped {\n"
                "    func `default`() -> Int {\n"
                "        return 1\n"
                "    }\n"
                "}\n"
            )
            resolved = memidx.resolve_symbol_to_path(code_root, "default")
            self.assertEqual(resolved, "Escaped.swift")

    def test_bare_symbol_resolution_finds_an_actor_container_name_the_old_regex_missed(self):
        """The old regex's keyword list (func/class/struct/enum/let/var)
        never included `actor` (or `protocol`/`extension`/`init`/
        `subscript`) at all -- a bare symbol naming an actor's own type
        could never resolve."""
        with tempfile.TemporaryDirectory() as td:
            code_root = Path(td) / "code"
            code_root.mkdir()
            (code_root / "Counter.swift").write_text(
                "actor Counter {\n"
                "    func increment() -> Int { return 1 }\n"
                "}\n"
            )
            resolved = memidx.resolve_symbol_to_path(code_root, "Counter")
            self.assertEqual(resolved, "Counter.swift")

    def test_code_index_fast_path_resolves_symbol_when_index_matches_code_root(self):
        """Finding 7: 'the code index when present' -- resolve_symbol_to_path
        must actually consult it (_resolve_symbol_via_code_index), not just
        fall back to scanning code_root every time. Isolated by DELETING the
        source file after indexing: only the fast path's chunks-table
        lookup -- never the fallback file scan -- can possibly still
        resolve this symbol."""
        with tempfile.TemporaryDirectory() as td:
            code_root = Path(td) / "code"
            code_root.mkdir()
            (code_root / "Escaped.swift").write_text(
                "class Escaped {\n"
                "    func `default`() -> Int {\n"
                "        return 1\n"
                "    }\n"
                "}\n"
            )
            project = "fastpath-proj"
            with mc_home(Path(td) / "home"):
                code_args = ns(
                    project=project, code_root=str(code_root), db=None,
                    no_embed=True, full=False, lang="swift",
                )
                self.assertEqual(memidx.cmd_code_reindex(code_args), 0)

                (code_root / "Escaped.swift").unlink()  # kill the fallback scan's only path

                resolved = memidx.resolve_symbol_to_path(code_root, "default", project=project)
            self.assertEqual(resolved, "Escaped.swift")

    def test_code_index_mismatched_root_falls_back_to_scan_not_a_stale_hit(self):
        """Finding 7: a code index built against a DIFFERENT code_root than
        the one being queried must never be trusted (its relative chunk
        paths would resolve against the wrong tree) -- resolve_symbol_to_path
        must fall back to scanning the REAL code_root instead, both for a
        symbol only the real tree has (still resolves) and one only the
        stale index has (must NOT be reported as a match)."""
        with tempfile.TemporaryDirectory() as td:
            indexed_root = Path(td) / "indexed-code"
            indexed_root.mkdir()
            (indexed_root / "Old.swift").write_text(
                "class Old {\n    func stale() -> Int { return 1 }\n}\n"
            )
            real_root = Path(td) / "real-code"
            real_root.mkdir()
            (real_root / "New.swift").write_text(
                "class New {\n    func fresh() -> Int { return 2 }\n}\n"
            )
            project = "mismatch-proj"
            with mc_home(Path(td) / "home"):
                code_args = ns(
                    project=project, code_root=str(indexed_root), db=None,
                    no_embed=True, full=False, lang="swift",
                )
                self.assertEqual(memidx.cmd_code_reindex(code_args), 0)

                resolved_real = memidx.resolve_symbol_to_path(real_root, "fresh", project=project)
                resolved_stale = memidx.resolve_symbol_to_path(real_root, "stale", project=project)
            self.assertEqual(resolved_real, "New.swift")
            self.assertIsNone(resolved_stale)

    def test_why_json_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                symbol_or_path="Sources/Delete/DeleteGate.swift", code_root=None, json=True,
            )
            rc, out = run_capturing(memidx.cmd_why, args)
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertTrue(any(c["id"] == "CON-007" for c in data))


# ---------------------------------------------------------------------------
# 4. drift
# ---------------------------------------------------------------------------


class TestDrift(unittest.TestCase):
    def _root_and_db(self, td):
        root = build_root(td)
        db = Path(td) / "idx.sqlite"
        reindex(root, db)
        return root, db

    def test_drift_red_detects_bypass(self):
        with tempfile.TemporaryDirectory() as td:
            _root, db = self._root_and_db(td)
            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                code_root=str(FIXTURES / "code"), json=True,
            )
            rc, out = run_capturing(memidx.cmd_drift, args)
            self.assertEqual(rc, 1)
            data = json.loads(out)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["topic"], "TOP-0100")
            self.assertEqual(data[0]["link"], "L2")
            self.assertEqual(len(data[0]["hits"]), 1)
            self.assertIn("QuickCleanup.swift", data[0]["hits"][0])

    def test_drift_green_when_bypass_routed_through_gate(self):
        with tempfile.TemporaryDirectory() as td:
            _root, db = self._root_and_db(td)
            fixed_code = Path(td) / "fixed_code"
            shutil.copytree(FIXTURES / "code", fixed_code)
            bad_file = fixed_code / "Sources" / "Scan" / "QuickCleanup.swift"
            text = bad_file.read_text()
            text = text.replace(
                "try FileManager.default.removeItem(atPath: path)",
                "try DeleteGate.delete(path)",
            )
            bad_file.write_text(text)

            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                code_root=str(fixed_code), json=True,
            )
            rc, out = run_capturing(memidx.cmd_drift, args)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out), [])

    def test_drift_text_report_format(self):
        with tempfile.TemporaryDirectory() as td:
            _root, db = self._root_and_db(td)
            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                code_root=str(FIXTURES / "code"), json=False,
            )
            rc, out = run_capturing(memidx.cmd_drift, args)
            self.assertEqual(rc, 1)
            self.assertIn("DRIFT: TOP-0100/L2", out)
            self.assertIn("hits outside allowed", out)
            self.assertRegex(out, r"QuickCleanup\.swift:\d+")

    def test_drift_ignores_git_and_build_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            _root, db = self._root_and_db(td)
            code_root = Path(td) / "code_with_junk"
            shutil.copytree(FIXTURES / "code", code_root)
            junk_git = code_root / ".git"
            junk_git.mkdir()
            (junk_git / "extra.swift").write_text('FileManager.default.removeItem(atPath: "x")\n')
            junk_build = code_root / ".build"
            junk_build.mkdir()
            (junk_build / "extra2.swift").write_text('FileManager.default.removeItem(atPath: "y")\n')

            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                code_root=str(code_root), json=True,
            )
            rc, out = run_capturing(memidx.cmd_drift, args)
            self.assertEqual(rc, 1)
            data = json.loads(out)
            self.assertEqual(len(data[0]["hits"]), 1)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCLIWiring(unittest.TestCase):
    def test_main_dispatches_why_and_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db)
            rc = memidx.main(["why", "--db", str(db), "Sources/Delete/DeleteGate.swift"])
            self.assertEqual(rc, 0)
            rc = memidx.main(
                ["drift", "--db", str(db), "--code-root", str(FIXTURES / "code"), "--json"]
            )
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
