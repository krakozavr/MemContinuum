"""Tests for Anatomy's intent index: memidx.py's `code-reindex`/`code-search`
(a lexer-aware Swift chunker + a completely separate <project>-code.sqlite
index) and memlint.py's concept-record rule additions.

Owned by this session (per the concurrent-work split, see the repo-root
brief): memidx.py's code-index additions, memlint.py's concept-rule
additions, this file, and fixtures/code/. Does not touch hooks/, scripts/repo-init.sh,
tests/test_hooks.py, tests/test_write_hooks.py, or tests/test_repo_init.py.

Every python invocation for this project is
`PYTHONPATH= $MEMCONTINUUM_PYTHON` (a Windows numpy install leaks onto
PYTHONPATH by default in this shell and breaks fastembed under Linux Python
-- see the module import failing with `AttributeError: module 'os' has no
attribute 'add_dll_directory'` if that env var isn't cleared). This
machine's venv python is never hardcoded in tracked test code -- set
$MEMCONTINUUM_PYTHON in your own (untracked) shell environment before
running this file; see README.md "Requirements" / "Running the tests".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

FIXTURES = TOOLS_DIR / "fixtures" / "code"

# The real-corpus gold-probe test below runs against a PRIVATE code tree. Its
# corpus path, its probe queries and the qualified names it expects all
# describe that tree, so none of them may live in tracked content (privacy
# requirement, tests/test_repo_init.py TestNoMachineIdentifyingContent). They are
# read at run time from an untracked TSV instead; without it the test skips.
PROBE_FILE = Path(
    os.environ.get("MEMCONTINUUM_TEST_PROBES", TOOLS_DIR / "docs" / "internal" / "gold-probes.tsv")
)


def load_probe_set(path=PROBE_FILE):
    """-> (corpus_root, [(query, expected_qualified_name)], min_top1), or None
    when the untracked probe file is absent or unusable.

    Format. Header directives `#corpus:` and `#min_top1:` are BOTH required --
    defaulting either one is how a threshold quietly becomes vacuous (a missing
    min_top1 defaulting to 1) or a reindex quietly targets the working
    directory (an empty corpus resolving to Path(".")). First directive wins on
    a repeat, matching scripts/codanna-bench.sh's `sed | head -1`.

    Data rows are TAB-separated: query, qualified_name, negative_substr
    (bench only) and gold_flag. Every column is always present and "-" means
    none: an EMPTY column is unusable, because TAB is an IFS whitespace
    character and bash `read` collapses consecutive tabs, silently shifting a
    later column into an earlier variable. Only gold rows reach the assertion --
    the bench runs a wider probe list, and folding the extras in would let a
    hit on a bench-only probe mask a miss on a gold one against an absolute
    threshold.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    corpus, min_top1, probes = None, None, []
    corpus_seen = min_top1_seen = False
    for line in text.splitlines():
        if line.startswith("#corpus:"):
            # First directive wins EVEN when its value is empty -- otherwise
            # an empty first line lets a later one through here while the
            # bench's `sed | head -1` keeps the empty first, and the two
            # consumers disagree about the same file (round-3 finding 11).
            if not corpus_seen:
                corpus_seen = True
                value = line.split(":", 1)[1].strip()
                if value:
                    corpus = Path(value).expanduser()
        elif line.startswith("#min_top1:"):
            if not min_top1_seen:
                min_top1_seen = True
                try:
                    min_top1 = int(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
                if min_top1 < 1:
                    # 0 would make the assertion vacuous.
                    return None
        elif line.startswith("#") or not line.strip():
            continue
        else:
            cols = line.split("\t")
            if len(cols) >= 2 and cols[0].strip() and cols[1].strip():
                is_gold = len(cols) >= 4 and cols[3].strip().lower() == "gold"
                if is_gold:
                    probes.append((cols[0].strip(), cols[1].strip()))
    if corpus is None or min_top1 is None or not probes:
        return None
    return corpus, probes, min_top1


def ns(**kw):
    base = dict(project=memidx.DEFAULT_PROJECT, db=None)
    base.update(kw)
    return SimpleNamespace(**base)


def code_reindex(code_root, db, project=memidx.DEFAULT_PROJECT, no_embed=True, full=False, lang=None):
    args = ns(code_root=str(code_root), db=str(db), project=project, no_embed=no_embed, full=full, lang=lang)
    return memidx.cmd_code_reindex(args)


# ---------------------------------------------------------------------------
# 1. Chunker fixtures (memidx.chunk_source directly -- no DB involved)
# ---------------------------------------------------------------------------


class TestChunkerNestedTypesAndExtension(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "NestedTypes.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_nested_func_qualifies_through_both_types(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Outer.Inner.innerFunc", names)

    def test_extension_qualifies_like_its_extended_type(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Outer.extFunc", names)

    def test_class_and_struct_are_never_chunks_themselves(self):
        kinds = {c["kind"] for c in self.chunks}
        self.assertFalse(kinds & {"class", "struct", "enum", "protocol", "extension"})
        symbols = {c["symbol"] for c in self.chunks}
        self.assertNotIn("Outer", symbols)
        self.assertNotIn("Inner", symbols)


class TestChunkerLazyAndComputedVar(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "LazyAndComputedVar.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_lazy_var_with_closure_initializer_excluded(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertNotIn("Cache.cached", names)

    def test_stored_var_with_plain_initializer_excluded(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertNotIn("Cache.stored", names)

    def test_computed_var_with_body_included(self):
        names = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Cache.computed", names)
        self.assertEqual(names["Cache.computed"]["kind"], "var")


class TestChunkerStringsAndInterpolation(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "StringsAndInterpolation.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        # a '{'/'}' inside a plain string, and a closure inside a string
        # interpolation, must never desync the walker.
        self.assertEqual(self.gaps, [])

    def test_brace_in_string_does_not_split_the_function(self):
        names = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Formatter.greet", names)
        greet = names["Formatter.greet"]
        self.assertEqual(greet["start_line"], 9)
        self.assertEqual(greet["end_line"], 13)

    def test_interpolation_closure_balances_correctly(self):
        # if \(items.map { ... }.joined()) mis-balanced brace counting,
        # greet()'s own close would be found in the wrong place (already
        # covered above) or raw()/multiline() wouldn't be found at all.
        names = {c["qualified_name"] for c in self.chunks}
        self.assertEqual(names, {"Formatter.greet", "Formatter.rawPath", "Formatter.multiline"})

    def test_raw_and_triple_quoted_strings_do_not_desync(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Formatter.rawPath", names)
        self.assertIn("Formatter.multiline", names)


class TestChunkerInitSubscriptOperator(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "InitSubscriptOperator.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_init_chunked_and_qualified(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Vec.init", by_name)
        self.assertEqual(by_name["Vec.init"]["kind"], "init")

    def test_subscript_has_no_own_name_but_chunks(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Vec.subscript", by_name)
        self.assertEqual(by_name["Vec.subscript"]["kind"], "subscript")

    def test_static_operator_func_chunks(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Vec.==", by_name)


class TestChunkerIfBranchesKeptBothSides(unittest.TestCase):
    def test_both_if_and_else_definitions_are_chunked(self):
        text = (FIXTURES / "IfBranches.swift").read_text()
        chunks, gaps = memidx.chunk_source(text)
        self.assertEqual(gaps, [])
        mode_chunks = [c for c in chunks if c["qualified_name"] == "Debugger.mode"]
        self.assertEqual(len(mode_chunks), 2, chunks)


class TestChunkerGapFallback(unittest.TestCase):
    """GapDesync.swift has a deliberately unbalanced #if/#else pair (a
    stray extra '}'). The chunker must skip THAT GAP (counted + reported)
    and keep indexing the rest of the file on BOTH sides of it -- never a
    whole-file fallback."""

    def setUp(self):
        self.text = (FIXTURES / "GapDesync.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_exactly_one_gap_reported(self):
        self.assertEqual(len(self.gaps), 1, self.gaps)
        start, end = self.gaps[0]
        self.assertLess(start, end)

    def test_content_before_the_gap_still_indexed(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("WellFormed.before", names)

    def test_content_after_the_gap_still_indexed(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("AlsoWellFormed.trailing", names)

    def test_never_falls_back_to_zero_chunks(self):
        self.assertGreater(len(self.chunks), 0)


class TestChunkerSyntheticAppKitFixture(unittest.TestCase):
    """~200-line synthetic AppKit-style controller (modeled on, not copied
    from, a real file -- this repo's tracked fixtures stay generic)."""

    def setUp(self):
        path = FIXTURES / "SyntheticSettingsController.swift"
        with path.open() as fh:
            self.line_count = sum(1 for _ in fh)
        self.text = path.read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_fixture_is_roughly_200_lines(self):
        self.assertGreater(self.line_count, 150)

    def test_no_gaps_on_realistic_appkit_shaped_source(self):
        self.assertEqual(self.gaps, [])

    def test_wrap_in_scroll_and_write_png_style_helpers_found(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("SyntheticSettingsController.embedInScrollBox", names)
        self.assertIn("SyntheticSettingsController.writePNGSnapshot", names)

    def test_computed_var_and_stored_property_both_handled_correctly(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("SyntheticSettingsController.rowCount", names)
        # `placeholderView` is a `lazy var ... = { ... }()` -- excluded.
        self.assertNotIn("SyntheticSettingsController.placeholderView", names)

    def test_a_reasonable_number_of_chunks_recovered(self):
        self.assertGreaterEqual(len(self.chunks), 15, self.chunks)


class TestChunkerActorObserversExtensionBacktick(unittest.TestCase):
    """Finding 6: actor as a container (qualifies like class/struct/enum/
    protocol/extension, never a chunk itself), willSet/didSet observers on
    a stored property are NOT computed-var chunks, `extension Outer.Inner`
    keeps its dotted qualifier, and backtick-quoted names are captured."""

    def setUp(self):
        self.text = (FIXTURES / "ChunkerCases.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_actor_qualifies_its_members_and_is_never_a_chunk_itself(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Counter.increment", names)
        kinds = {c["kind"] for c in self.chunks}
        self.assertNotIn("actor", kinds)
        symbols = {c["symbol"] for c in self.chunks}
        self.assertNotIn("Counter", symbols)

    def test_willset_didset_observers_are_not_a_computed_var_chunk(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertNotIn("Observed.value", names)

    def test_extension_of_a_nested_type_keeps_the_dotted_qualifier(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Outer.Inner.nested", names)

    def test_backtick_quoted_func_and_var_names_are_captured(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Escaped.default", by_name)
        self.assertEqual(by_name["Escaped.default"]["kind"], "func")
        self.assertIn("Escaped.type", by_name)
        self.assertEqual(by_name["Escaped.type"]["kind"], "var")


class TestChunkerGapResyncModifiers(unittest.TestCase):
    """Finding 6: gap-resync must also recognise `package`/`consuming`/
    `borrowing` func modifiers and bare `var`/`subscript` declarations as
    valid resync anchors -- previously the heuristic only knew func/init/
    class/struct/enum/protocol/extension (with a narrower modifier set),
    so a member using this vocabulary right after a desync used to be
    swallowed into the same gap instead of being recovered."""

    def setUp(self):
        self.text = (FIXTURES / "GapResyncModifiers.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_exactly_five_gaps_reported(self):
        self.assertEqual(len(self.gaps), 5, self.gaps)

    def test_package_modifier_recovered_after_gap(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("afterPackageGap", names)

    def test_consuming_modifier_recovered_after_gap(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("afterConsumingGap", names)

    def test_borrowing_modifier_recovered_after_gap(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("afterBorrowingGap", names)

    def test_bare_var_recovered_after_gap(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("afterVarGap", by_name)
        self.assertEqual(by_name["afterVarGap"]["kind"], "var")

    def test_bare_subscript_recovered_after_gap(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("subscript", by_name)
        self.assertEqual(by_name["subscript"]["kind"], "subscript")


class TestChunkerClassMemberAndBacktickContainer(unittest.TestCase):
    """Finding 3: `class subscript` (and other `class <modifier> func/var/
    subscript` member forms) is a class-level MEMBER, not a phantom type
    container -- only `class func`/`class var` used to be excluded. A
    backtick-quoted extension target must still be kept as a real
    container, including when it is itself a dotted, backtick-quoted
    nested type."""

    def setUp(self):
        self.text = (FIXTURES / "ClassMemberAndBacktickContainer.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_class_subscript_qualifies_under_box_not_under_a_phantom_subscript_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Box.subscript", names)
        self.assertNotIn("Box.subscript.subscript", names)

    def test_class_subscript_is_not_a_type_container(self):
        symbols = {c["symbol"] for c in self.chunks}
        # a phantom "subscript" container would make "make" qualify as
        # Box.subscript.make instead of Box.make.
        self.assertIn("Box.make", {c["qualified_name"] for c in self.chunks})

    def test_class_final_func_member_form_is_not_a_phantom_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Box.make", names)
        self.assertNotIn("Box.final.make", names)
        symbols = {c["symbol"] for c in self.chunks}
        self.assertNotIn("final", symbols)

    def test_backtick_extension_target_keeps_its_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Type.plain", names)

    def test_backtick_dotted_nested_extension_target_keeps_its_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Type.Inner.nested", names)


# ---------------------------------------------------------------------------
# 2. code-reindex / code-search (DB-backed)
# ---------------------------------------------------------------------------


class TestIncremental(unittest.TestCase):
    def test_unchanged_file_gives_zero_reembeds(self):
        """Finding 8: despite its name, this test used to run with
        embeddings OFF both times (no_embed=True) and only checked that
        the chunk COUNT was unchanged -- it never actually asserted a
        re-embed count, so it couldn't tell "0 reembeds because nothing
        changed" apart from "0 reembeds because embedding never ran at
        all". Turn embeddings ON (no_embed=False) for this small (3-func)
        fixture and assert the real re-embed count from code-reindex's own
        summary line: > 0 on the first run, exactly 0 on the second
        (sha-skip, file byte-for-byte unchanged)."""
        import contextlib
        import io
        import re

        reembed_re = re.compile(r"(\d+) chunk\(s\) \(re-\)embedded")

        def reembed_count(root, db):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=False)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            m = reembed_re.search(out)
            self.assertIsNotNone(m, out)
            return int(m.group(1))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            first = reembed_count(root, db)
            self.assertGreater(first, 0, "first-ever index of a new file must embed its chunks")

            second = reembed_count(root, db)
            self.assertEqual(second, 0, "file byte-for-byte unchanged -- sha-skip must re-embed nothing")

    def test_changed_file_is_rechunked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            target.write_text(target.read_text() + "\nfunc addedLater() {}\n")
            code_reindex(root, db, no_embed=True)

            conn = memidx.open_code_db(db)
            rows = conn.execute("SELECT qualified_name FROM chunks").fetchall()
            conn.close()
            self.assertIn("addedLater", {r["qualified_name"] for r in rows})


class TestIsolationFromMarkdownIndex(unittest.TestCase):
    def test_code_reindex_never_creates_the_markdown_project_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")

            args = ns(code_root=str(root), project="isoproj", no_embed=True, full=False, lang=None)
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                rc = memidx.cmd_code_reindex(args)
            finally:
                del os.environ["MEMCONTINUUM_HOME"]
            self.assertEqual(rc, 0)

            self.assertTrue((home / "isoproj-code.sqlite").exists())
            self.assertFalse((home / "isoproj.sqlite").exists())

    def test_code_db_path_differs_from_markdown_db_path(self):
        code_args = ns(project="samename", db=None)
        md_args = ns(project="samename", db=None)
        os.environ["MEMCONTINUUM_HOME"] = "/tmp/does-not-need-to-exist-for-this-check"
        try:
            code_path = memidx.resolve_code_db_path(code_args)
            md_path = memidx.resolve_db_path(md_args)
        finally:
            del os.environ["MEMCONTINUUM_HOME"]
        self.assertNotEqual(code_path, md_path)


class TestLazyImportSubprocessAssert(unittest.TestCase):
    """Isolated-process checks: `code-reindex --no-embed` and `code-search
    --mode fts` must never import fastembed (or numpy via it) at all --
    same pattern as test_memidx.py's for-path check."""

    def test_code_reindex_no_embed_does_not_import_fastembed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-reindex', '--db', %r, '--code-root', %r, '--no-embed']); "
                "assert 'fastembed' not in sys.modules, 'fastembed was imported'; "
                "assert 'numpy' not in sys.modules, 'numpy was imported'"
            ) % (str(TOOLS_DIR), str(db), str(root))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_code_search_fts_mode_does_not_import_fastembed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts']); "
                "assert 'fastembed' not in sys.modules, 'fastembed was imported'; "
                "assert 'numpy' not in sys.modules, 'numpy was imported'"
            ) % (str(TOOLS_DIR), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestFTSSplitFindsCamelCase(unittest.TestCase):
    def test_write_png_finds_writePNG_shaped_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(
                FIXTURES / "SyntheticSettingsController.swift",
                root / "SyntheticSettingsController.swift",
            )
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            args = ns(db=str(db), query="write png", mode="fts", limit=5, json=True)
            rc = memidx.cmd_code_search(args)
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            ids = memidx.code_hits_fts(conn, "write png", memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertGreater(len(ids), 0)
            conn = memidx.open_code_db(db)
            top = conn.execute("SELECT qualified_name FROM chunks WHERE id=?", (ids[0],)).fetchone()
            conn.close()
            self.assertIn("writePNGSnapshot", top["qualified_name"])


class TestVectorAndHybridJSONIsSerializable(unittest.TestCase):
    """Regression test: cosine() used to return a numpy.float32 score
    (fastembed's query_embed yields numpy arrays), which json.dumps cannot
    serialize -- `code-search --mode vector/hybrid --json` crashed with
    TypeError: Object of type float32 is not JSON serializable the moment
    it tried to print. Control-experiment verified: this test fails on the
    pre-fix cosine() (reverting the `float(...)` cast in memidx.py
    reproduces the crash on the vector-mode test below; confirmed by
    actually running it against the pre-fix code). --mode fts never
    touches cosine() at all, so it isn't a control for this bug; hybrid's
    own output score is an RRF rank sum (always a plain float) rather than
    a raw cosine value, so it was never actually at risk from this
    specific bug -- its test here is a straightforward correctness check,
    not a second control case."""

    def _search_json(self, db, mode):
        # Finding 8: the old version only asserted "OK" appeared in
        # stdout and that every score (already round-tripped through
        # json.loads, which never yields numpy types either way -- the
        # real risk is json.dumps() raising before that point) was a
        # plain float -- neither check could fail even if `out` were
        # empty, silently losing all coverage of the actual float32 path.
        # Assert a nonzero hit count explicitly instead.
        script = (
            "import sys, io, contextlib, json; sys.path.insert(0, %r); import memidx; "
            "buf = io.StringIO()\n"
            "with contextlib.redirect_stdout(buf):\n"
            "    rc = memidx.main(['code-search', '--db', %r, 'embed a view in a scroll box', "
            "'--mode', %r, '--limit', '3', '--json'])\n"
            "assert rc == 0, rc\n"
            "env = json.loads(buf.getvalue())\n"
            "assert env['state'] == 'current', env['state']\n"
            "out = env['results']\n"
            "assert len(out) > 0, 'zero hits -- the float32 path was never exercised'\n"
            "for h in out:\n"
            "    assert isinstance(h['score'], float), (h['score'], type(h['score']))\n"
            "print('OK', len(out))\n"
        ) % (str(TOOLS_DIR), str(db), mode)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
        hit_count = int(result.stdout.strip().rsplit(" ", 1)[-1])
        self.assertGreater(hit_count, 0, result.stdout)
        return hit_count

    def test_vector_mode_json_output_is_valid_and_scores_are_plain_floats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(
                FIXTURES / "SyntheticSettingsController.swift",
                root / "SyntheticSettingsController.swift",
            )
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=False)
            self._search_json(db, "vector")

    def test_hybrid_mode_json_output_is_valid_and_scores_are_plain_floats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(
                FIXTURES / "SyntheticSettingsController.swift",
                root / "SyntheticSettingsController.swift",
            )
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=False)
            self._search_json(db, "hybrid")

            # Finding 8: hybrid's own per-hit score is an RRF rank sum
            # (always a plain float already, per the class docstring), so
            # a passing hybrid JSON test alone doesn't prove the merge
            # actually drew on real vector-side (float32-cast) results
            # rather than FTS alone. Confirm code_hits_vector -- the exact
            # function that used to leak a numpy.float32 score -- genuinely
            # returns nonempty, plain-float results for this same query.
            conn = memidx.open_code_db(db)
            try:
                vec_hits = memidx.code_hits_vector(
                    conn, "embed a view in a scroll box", memidx.DEFAULT_PROJECT
                )
            finally:
                conn.close()
            self.assertGreater(len(vec_hits), 0, "hybrid's vector side never ran")
            for _cid, score in vec_hits:
                self.assertIsInstance(score, float)


class TestConceptAttachment(unittest.TestCase):
    def test_hit_gets_concept_id_when_decision_db_has_a_matching_implemented_by(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                md_root = Path(td) / "md"
                md_root.mkdir()
                (md_root / "concept.md").write_text(
                    "---\n"
                    "type: concept\n"
                    "id: CON-ATTACH\n"
                    "title: Attach Test\n"
                    "owner_boundary: fixture\n"
                    "implemented_by:\n"
                    "  - NestedTypes.swift#outerFunc\n"
                    "tested_by: []\n"
                    "governed_by: []\n"
                    "involved_in: []\n"
                    "---\n\n"
                    "Fixture. NOT this concept: nothing else.\n"
                )
                memidx.cmd_reindex(ns(root=str(md_root), no_embed=True, full=False))

                code_root = Path(td) / "code"
                code_root.mkdir()
                shutil.copy(FIXTURES / "NestedTypes.swift", code_root / "NestedTypes.swift")
                memidx.cmd_code_reindex(ns(code_root=str(code_root), no_embed=True, full=False, lang=None))

                import io
                import contextlib

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = memidx.cmd_code_search(ns(query="outer func", mode="fts", limit=5, json=True))
                self.assertEqual(rc, 0)
                import json as _json

                results = _json.loads(buf.getvalue())["results"]
                self.assertTrue(any(r.get("concept_id") == "CON-ATTACH" for r in results), results)
            finally:
                del os.environ["MEMCONTINUUM_HOME"]

    def test_explicit_code_db_still_attaches_concepts_from_decision_db_and_stays_clean(self):
        """Finding 3: `code-search --db <codedb>` used to run the markdown
        SCHEMA_SQL onto the CODE db (schema clash, concepts read from the
        wrong db, attach silently empty). `--db` for code-search must
        select the CODE db only; concept attachment must always read the
        DECISION db (default project path here -- no --decision-db
        override needed for this case)."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                md_root = Path(td) / "md"
                md_root.mkdir()
                (md_root / "concept.md").write_text(
                    "---\n"
                    "type: concept\n"
                    "id: CON-EXPLICITDB\n"
                    "title: Explicit DB Test\n"
                    "owner_boundary: fixture\n"
                    "implemented_by:\n"
                    "  - NestedTypes.swift#outerFunc\n"
                    "tested_by: []\n"
                    "governed_by: []\n"
                    "involved_in: []\n"
                    "---\n\n"
                    "Fixture. NOT this concept: nothing else.\n"
                )
                memidx.cmd_reindex(ns(root=str(md_root), no_embed=True, full=False))

                code_root = Path(td) / "code"
                code_root.mkdir()
                shutil.copy(FIXTURES / "NestedTypes.swift", code_root / "NestedTypes.swift")
                code_db = Path(td) / "explicit-code.sqlite"
                memidx.cmd_code_reindex(
                    ns(code_root=str(code_root), db=str(code_db), no_embed=True, full=False, lang=None)
                )

                import io
                import contextlib
                import json as _json

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = memidx.cmd_code_search(
                        ns(query="outer func", mode="fts", limit=5, json=True, db=str(code_db))
                    )
                self.assertEqual(rc, 0)
                results = _json.loads(buf.getvalue())["results"]
                self.assertTrue(
                    any(r.get("concept_id") == "CON-EXPLICITDB" for r in results), results
                )

                import sqlite3

                code_conn = sqlite3.connect(str(code_db))
                tables = {
                    r[0]
                    for r in code_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                code_conn.close()
                markdown_only_tables = {"records", "links", "edges", "assumptions", "concepts", "concept_paths"}
                self.assertFalse(
                    tables & markdown_only_tables,
                    f"code db must never carry markdown tables: {tables & markdown_only_tables}",
                )
            finally:
                del os.environ["MEMCONTINUUM_HOME"]

    def test_two_concepts_on_one_file_different_symbols_each_get_their_own_chunk(self):
        """Finding 4: concept_id attachment must prefer a symbol-level
        match -- an implemented_by entry carrying #symbol must only stamp
        the chunk(s) whose qualified name actually matches that symbol,
        not every chunk in the file."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                md_root = Path(td) / "md"
                md_root.mkdir()
                (md_root / "concept_outer.md").write_text(
                    "---\n"
                    "type: concept\n"
                    "id: CON-OUTER\n"
                    "title: Outer func concept\n"
                    "owner_boundary: fixture\n"
                    "implemented_by:\n"
                    "  - NestedTypes.swift#outerFunc\n"
                    "tested_by: []\n"
                    "governed_by: []\n"
                    "involved_in: []\n"
                    "---\n\n"
                    "Fixture. NOT this concept: nothing else.\n"
                )
                (md_root / "concept_ext.md").write_text(
                    "---\n"
                    "type: concept\n"
                    "id: CON-EXT\n"
                    "title: Ext func concept\n"
                    "owner_boundary: fixture\n"
                    "implemented_by:\n"
                    "  - NestedTypes.swift#extFunc\n"
                    "tested_by: []\n"
                    "governed_by: []\n"
                    "involved_in: []\n"
                    "---\n\n"
                    "Fixture. NOT this concept: nothing else.\n"
                )
                memidx.cmd_reindex(ns(root=str(md_root), no_embed=True, full=False))

                code_root = Path(td) / "code"
                code_root.mkdir()
                shutil.copy(FIXTURES / "NestedTypes.swift", code_root / "NestedTypes.swift")
                memidx.cmd_code_reindex(ns(code_root=str(code_root), no_embed=True, full=False, lang=None))

                import io
                import contextlib
                import json as _json

                def top_hit(query):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        rc = memidx.cmd_code_search(ns(query=query, mode="fts", limit=5, json=True))
                    self.assertEqual(rc, 0)
                    return _json.loads(buf.getvalue())["results"][0]

                outer_hit = top_hit("outer func")
                self.assertEqual(outer_hit["qualified_name"], "Outer.outerFunc")
                self.assertEqual(outer_hit.get("concept_id"), "CON-OUTER", outer_hit)

                ext_hit = top_hit("ext func")
                self.assertEqual(ext_hit["qualified_name"], "Outer.extFunc")
                self.assertEqual(ext_hit.get("concept_id"), "CON-EXT", ext_hit)
            finally:
                del os.environ["MEMCONTINUUM_HOME"]


class TestStaleWarning(unittest.TestCase):
    def test_touching_a_source_file_triggers_stderr_warning_on_search(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys, time, os; sys.path.insert(0, %r); import memidx; "
                "os.utime(%r, (time.time() + 3600, time.time() + 3600)); "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts'])"
            ) % (str(TOOLS_DIR), str(target), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertIn("stale", result.stderr.lower(), result.stderr)

    def test_touch_then_reindex_with_unchanged_sha_clears_stale_warning(self):
        """Finding 2: a SHA-skipped unchanged file must still refresh its
        stored file_sha mtime/size row -- otherwise the on-disk mtime keeps
        drifting away from the stale-comparison snapshot forever after a
        mere touch, even though a reindex genuinely ran and found nothing
        to do. touch -> reindex (sha unchanged, 0 chunks touched) -> search
        must NOT warn stale."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            os.utime(target, (time.time() + 3600, time.time() + 3600))
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts'])"
            ) % (str(TOOLS_DIR), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertNotIn("stale", result.stderr.lower(), result.stderr)

    def test_search_never_warns_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts'])"
            ) % (str(TOOLS_DIR), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertNotIn("stale", result.stderr.lower(), result.stderr)


class TestIndexProvenance(unittest.TestCase):
    """Finding 1 (HIGH): code-search must distinguish three code-index
    states and SAY so -- uninitialized (never reindexed for this project:
    refuse, not a healthy-empty result), stale (existing warning, now also
    surfaced in --json), current (indexed_at/code_root/head_sha surfaced
    in --json)."""

    def _run(self, argv):
        script = (
            "import sys; sys.path.insert(0, %r); import memidx; "
            "sys.exit(memidx.main(%r))"
        ) % (str(TOOLS_DIR), argv)
        return subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )

    def test_uninitialized_index_refuses_not_a_bare_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "never-reindexed-code.sqlite"
            result = self._run(["code-search", "--db", str(db), "anything", "--mode", "fts"])
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("code-reindex", (result.stdout + result.stderr).lower(), result.stderr)
            self.assertNotEqual(result.stdout.strip(), "[]")

    def test_uninitialized_index_json_carries_structured_state_not_bare_list(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "never-reindexed-code.sqlite"
            result = self._run(["code-search", "--db", str(db), "anything", "--mode", "fts", "--json"])
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["state"], "uninitialized")
            self.assertEqual(data["results"], [])

    def test_current_index_json_carries_indexed_at_and_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            result = self._run(["code-search", "--db", str(db), "outer func", "--mode", "fts", "--json"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["state"], "current")
            self.assertEqual(data["code_root"], str(root))
            self.assertIsInstance(data["indexed_at"], (int, float))
            self.assertGreater(len(data["results"]), 0)

    def test_stale_index_json_carries_state_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)
            os.utime(target, (time.time() + 3600, time.time() + 3600))

            result = self._run(["code-search", "--db", str(db), "outer func", "--mode", "fts", "--json"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["state"], "stale")

    def test_code_meta_stores_head_sha_when_code_root_is_a_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.email=t@t.t", "-c", "user.name=t",
                 "add", "-A"], check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.email=t@t.t", "-c", "user.name=t",
                 "commit", "-q", "-m", "init"], check=True,
            )
            expected_sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            conn = memidx.open_code_db(db)
            row = conn.execute(
                "SELECT head_sha FROM code_meta WHERE project=?", (memidx.DEFAULT_PROJECT,)
            ).fetchone()
            conn.close()
            self.assertEqual(row["head_sha"], expected_sha)


# ---------------------------------------------------------------------------
# 3. Budget tests
# ---------------------------------------------------------------------------


def _build_synthetic_swift_tree(root: Path, n: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    template = FIXTURES / "SyntheticSettingsController.swift"
    text = template.read_text()
    for i in range(n):
        # give each file a distinct top-level type name so none collide.
        variant = text.replace("SyntheticSettingsController", f"SyntheticController{i}")
        (root / f"Synthetic{i}.swift").write_text(variant)


class TestBudgets(unittest.TestCase):
    def test_no_embed_reindex_under_5s_on_173_synthetic_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            _build_synthetic_swift_tree(root, 173)
            db = Path(td) / "idx-code.sqlite"

            t0 = time.time()
            rc = code_reindex(root, db, no_embed=True)
            elapsed = time.time() - t0
            self.assertEqual(rc, 0)
            self.assertLess(elapsed, 5.0, f"code-reindex took {elapsed:.3f}s")

    def test_in_process_fts_search_under_100ms(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            _build_synthetic_swift_tree(root, 50)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            conn = memidx.open_code_db(db)
            t0 = time.time()
            ids = memidx.code_hits_fts(conn, "wrap scroll", memidx.DEFAULT_PROJECT)
            elapsed = time.time() - t0
            conn.close()
            self.assertGreater(len(ids), 0)
            self.assertLess(elapsed, 0.1, f"fts search took {elapsed:.3f}s")


# ---------------------------------------------------------------------------
# 4. Real-corpus gold-probe test (gated, like test_memidx's D5/D8 sandbox
#    tests, behind an env var -- skipped when unset)
# ---------------------------------------------------------------------------




class TestLoadProbeSet(unittest.TestCase):
    """Runs in the DEFAULT suite. The real-corpus test that consumes this
    parser is double-gated and normally skips, so without these the parser
    would never execute in CI at all (reviewer finding 6, 2026-08-31)."""

    def write(self, body):
        d = tempfile.mkdtemp()
        f = Path(d) / "probes.tsv"
        f.write_text(body, encoding="utf-8")
        return f

    GOOD = (
        "#corpus: /tmp\n"
        "#min_top1: 2\n"
        "a query\tPkg.alpha\t-\tgold\n"
        "b query\tPkg.beta\tbypass\tgold\n"
        "bench only\tPkg.gamma\t-\t-\n"
    )

    def test_parses_directives_and_gold_rows_only(self):
        corpus, probes, min_top1 = load_probe_set(self.write(self.GOOD))
        self.assertEqual(corpus, Path("/tmp"))
        self.assertEqual(min_top1, 2)
        self.assertEqual(probes, [("a query", "Pkg.alpha"), ("b query", "Pkg.beta")])

    def test_missing_file_is_none_not_an_error(self):
        self.assertIsNone(load_probe_set(Path("/nonexistent/probes.tsv")))

    def test_non_utf8_file_is_none_not_an_error(self):
        d = tempfile.mkdtemp()
        f = Path(d) / "probes.tsv"
        f.write_bytes(b"#corpus: /tmp\n\xff\xfe binary\n")
        self.assertIsNone(load_probe_set(f))

    def test_empty_corpus_directive_is_refused(self):
        """An empty value would resolve to Path(".") and point the real-corpus
        reindex at the working directory."""
        self.assertIsNone(load_probe_set(self.write(
            "#corpus:\n#min_top1: 1\nq\tP.a\t-\tgold\n")))

    def test_missing_min_top1_is_refused_rather_than_defaulted(self):
        """Defaulting it to 1 would make the assertion nearly vacuous."""
        self.assertIsNone(load_probe_set(self.write("#corpus: /tmp\nq\tP.a\t-\tgold\n")))

    def test_non_integer_min_top1_is_refused(self):
        self.assertIsNone(load_probe_set(self.write(
            "#corpus: /tmp\n#min_top1: five\nq\tP.a\t-\tgold\n")))

    def test_no_gold_rows_is_refused(self):
        self.assertIsNone(load_probe_set(self.write(
            "#corpus: /tmp\n#min_top1: 1\nq\tP.a\t-\t-\n")))

    def test_empty_first_corpus_directive_is_not_overridden_by_a_later_one(self):
        """First-wins must hold even for an empty value, or this parser and
        the bench's `sed | head -1` disagree about the same file."""
        self.assertIsNone(load_probe_set(self.write(
            "#corpus:\n#corpus: /tmp\n#min_top1: 1\nq\tP.a\t-\tgold\n")))

    def test_zero_min_top1_is_refused_as_vacuous(self):
        self.assertIsNone(load_probe_set(self.write(
            "#corpus: /tmp\n#min_top1: 0\nq\tP.a\t-\tgold\n")))

    def test_first_directive_wins_matching_the_bench_script(self):
        """scripts/codanna-bench.sh reads `sed ... | head -1`; last-wins here
        would make the two consumers disagree about the same file."""
        corpus, _, min_top1 = load_probe_set(self.write(
            "#corpus: /tmp\n#corpus: /var\n#min_top1: 3\n#min_top1: 9\n"
            "q\tP.a\t-\tgold\n"))
        self.assertEqual(corpus, Path("/tmp"))
        self.assertEqual(min_top1, 3)

    def test_comment_with_a_space_is_a_comment_not_a_directive(self):
        corpus, _, _ = load_probe_set(self.write(
            "# corpus: /decoy\n#corpus: /tmp\n#min_top1: 1\nq\tP.a\t-\tgold\n"))
        self.assertEqual(corpus, Path("/tmp"))


class TestGoldProbesRealCorpus(unittest.TestCase):
    """Hybrid top-1 accuracy against a real code corpus, not the fixtures.

    Double-gated: $MEMCONTINUUM_TEST_REAL_CORPUS must be set AND the untracked
    probe file must exist (see load_probe_set). Everything project-specific --
    corpus root, queries, expected qualified names, the pass threshold -- comes
    from that file, so this test names nothing private. The threshold is below
    100% on purpose: the misses are plausible near-misses (a sibling symbol or
    a semantically adjacent one taking top-1), not index bugs; the analysis of
    which probes miss and why is in the internal notes beside the probe file.
    """

    @unittest.skipUnless(
        os.environ.get("MEMCONTINUUM_TEST_REAL_CORPUS"),
        "set $MEMCONTINUUM_TEST_REAL_CORPUS=1 to run the real-corpus gold-probe test",
    )
    def test_hybrid_top1_on_real_corpus(self):
        loaded = load_probe_set()
        if loaded is None:
            self.skipTest(f"no probe set at {PROBE_FILE} (see $MEMCONTINUUM_TEST_PROBES)")
        corpus_root, probes, min_top1 = loaded
        self.assertTrue(corpus_root.is_dir(), corpus_root)
        # Cached (incrementally reused, like scripts/codanna-bench.sh's own
        # index) rather than a fresh tempdir -- a full embed of a real corpus
        # takes ~20+ minutes on this host, and re-embeds nothing once the
        # corpus hasn't changed since the last run.
        cache_dir = Path.home() / ".cache" / "codanna-bench"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db = cache_dir / "bench-code.sqlite"
        code_reindex(corpus_root, db, project="codanna-bench", no_embed=False, full=False)

        hits = 0
        details = []
        for query, expect in probes:
            conn = memidx.open_code_db(db)
            fts_ids = memidx.code_hits_fts(conn, query, "codanna-bench")
            vec_ids = [cid for cid, _ in memidx.code_hits_vector(conn, query, "codanna-bench")]
            k = 60
            scores: dict = {}
            for i, cid in enumerate(fts_ids):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
            for i, cid in enumerate(vec_ids):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
            ranked = sorted(scores.items(), key=lambda t: t[1], reverse=True)
            top1_id = ranked[0][0] if ranked else None
            top1 = None
            if top1_id is not None:
                row = conn.execute("SELECT qualified_name FROM chunks WHERE id=?", (top1_id,)).fetchone()
                top1 = row["qualified_name"] if row else None
            conn.close()
            ok = top1 == expect
            details.append((query, expect, top1, ok))
            if ok:
                hits += 1
        self.assertGreaterEqual(
            hits, min_top1, f"{hits}/{len(probes)} top-1 (threshold {min_top1}) -- {details}"
        )


# ---------------------------------------------------------------------------
# 5. memlint concept-rule additions (fixtures/code/concepts/)
# ---------------------------------------------------------------------------

CONCEPTS = FIXTURES / "concepts"
CODE_ROOT = FIXTURES


class TestMemlintSymbolFragment(unittest.TestCase):
    def test_symbol_not_declared_in_file_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_symbol.md", root / "bad_symbol.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-BADSYM" in e and "doesNotExist" in e for e in errors), errors
            )

    def test_symbol_declared_in_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("CON-CODE-GOOD" in e for e in errors), errors)

    def test_symbol_check_skipped_without_code_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_symbol.md", root / "bad_symbol.md")
            errors, _warnings = memlint.lint_root(root)
            self.assertFalse(any("doesNotExist" in e for e in errors), errors)


class TestMemlintSymbolVocabulary(unittest.TestCase):
    """Finding 5: memlint's #symbol check must recognize everything the
    chunker emits (init, subscript, computed var names, static/class func,
    operators, backtick names) and stop matching inside comments/strings."""

    def test_init_computed_var_static_func_and_backtick_name_all_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "vocab_good.md", root / "vocab_good.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("CON-VOCAB-GOOD" in e for e in errors), errors)

    def test_symbol_only_in_a_comment_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "vocab_bad_comment.md", root / "vocab_bad_comment.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-VOCAB-BADCOMMENT" in e and "commentedOutSymbol" in e for e in errors),
                errors,
            )

    def test_symbol_only_in_a_string_literal_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "vocab_bad_string.md", root / "vocab_bad_string.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-VOCAB-BADSTRING" in e and "stringOnlySymbol" in e for e in errors),
                errors,
            )


class TestMemlintFragmentAttachAgreement(unittest.TestCase):
    """Finding 4: memlint's #symbol vocabulary check must accept a QUALIFIED
    fragment (e.g. "Outer.outerFunc") exactly as code-search's runtime
    concept attachment (concept_matches_for_chunk) does -- one shared
    matcher (memidx.fragment_matches_symbol / fragment_declared_in_text),
    not two independently-encoded rules that can disagree."""

    def test_qualified_fragment_matching_runtime_attachment_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "qualified_symbol_good.md", root / "qualified_symbol_good.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("CON-CODE-QUALGOOD" in e for e in errors), errors)

    def test_lint_and_runtime_attach_agree_on_the_same_qualified_fragment(self):
        # The same predicate, exercised both ways on the same fixture data:
        # lint_concept accepts "Outer.outerFunc" against NestedTypes.swift
        # (above) AND concept_matches_for_chunk would match a chunk whose
        # symbol="outerFunc"/qualified_name="Outer.outerFunc" against a
        # concept_paths row fragment of "Outer.outerFunc" -- verified
        # directly via the shared predicate so a future regression that
        # breaks only one side is caught here.
        self.assertTrue(memidx.fragment_matches_symbol("Outer.outerFunc", "outerFunc", "Outer.outerFunc"))
        text = (FIXTURES / "NestedTypes.swift").read_text()
        self.assertTrue(memidx.fragment_declared_in_text("Outer.outerFunc", text))


class TestMemlintPathContainment(unittest.TestCase):
    """Finding 4: implemented_by/tested_by path resolution against code_root
    must be containment-checked -- an absolute ref_path used to silently
    discard code_root entirely (Path's `/` operator drops the left side for
    an absolute right-hand side), and a relative "../" path could walk
    outside code_root with no check at all."""

    def _lint_one_ref(self, ref_path: str, td: Path, code_root: Path):
        (td / "escape_concept.md").write_text(
            "---\n"
            "type: concept\n"
            "id: CON-CODE-ESCAPE\n"
            "title: Fixture -- path containment\n"
            "owner_boundary: fixture\n"
            "implemented_by:\n"
            f"  - {ref_path}\n"
            "tested_by: []\n"
            "governed_by: []\n"
            "involved_in: []\n"
            "---\n\n"
            "Fixture. NOT this concept: nothing else.\n"
        )
        return memlint.lint_root(td, code_root=code_root)

    def test_absolute_ref_path_escaping_code_root_is_an_error(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            outside = td / "secret.swift"
            outside.write_text("func real() {}\n")
            errors, _warnings = self._lint_one_ref(str(outside), td, code_root)
            self.assertTrue(
                any("CON-CODE-ESCAPE" in e and "absolute" in e for e in errors), errors
            )

    def test_relative_dotdot_escaping_code_root_is_an_error(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            outside = td / "secret.swift"
            outside.write_text("func real() {}\n")
            errors, _warnings = self._lint_one_ref("../secret.swift", td, code_root)
            self.assertTrue(
                any("CON-CODE-ESCAPE" in e and "escapes code_root" in e for e in errors), errors
            )

    def test_relative_path_inside_code_root_is_not_a_containment_error(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            (code_root / "Inside.swift").write_text("func real() {}\n")
            errors, _warnings = self._lint_one_ref("Inside.swift", td, code_root)
            self.assertFalse(
                any("CON-CODE-ESCAPE" in e and ("absolute" in e or "escapes" in e) for e in errors),
                errors,
            )


class TestMemlintUnqualifiedLargeFile(unittest.TestCase):
    def test_no_symbol_on_small_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "no_symbol_small.md", root / "no_symbol_small.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("CON-CODE-NOSYM-SMALL" in e for e in errors), errors)

    def test_no_symbol_on_a_file_over_400_lines_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code_root = Path(td) / "code"
            code_root.mkdir()
            big = code_root / "Big.swift"
            big.write_text("// filler line\n" * 401 + "func real() {}\n")
            (root / "big_concept.md").write_text(
                "---\n"
                "type: concept\n"
                "id: CON-CODE-BIG\n"
                "title: Big file, no symbol\n"
                "owner_boundary: fixture\n"
                "implemented_by:\n"
                "  - Big.swift\n"
                "tested_by: []\n"
                "governed_by: []\n"
                "involved_in: []\n"
                "---\n\n"
                "Fixture. NOT this concept: nothing else.\n"
            )
            errors, _warnings = memlint.lint_root(root, code_root=code_root)
            self.assertTrue(
                any("CON-CODE-BIG" in e and "400" in e for e in errors), errors
            )


class TestMemlintGovernedByRegistry(unittest.TestCase):
    def test_unknown_governed_by_id_is_error_when_topics_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_governed.md", root / "bad_governed.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-BADGOV" in e and "TOP-DOES-NOT-EXIST" in e for e in errors), errors
            )

    def test_known_governed_by_id_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("governed_by" in e and "CON-CODE-GOOD" in e for e in errors), errors)

    def test_governed_by_check_skipped_when_no_topic_registry_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_governed.md", root / "bad_governed.md")
            # deliberately NOT copying top-code-1.md -- zero topics in this
            # corpus, so there's no registry to validate against.
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("governed_by" in e for e in errors), errors)


class TestMemlintDuplicateClaim(unittest.TestCase):
    def test_two_concepts_claiming_the_same_symbol_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "dup_a.md", root / "dup_a.md")
            shutil.copy(CONCEPTS / "dup_b.md", root / "dup_b.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-DUP-A" in e and "CON-CODE-DUP-B" in e for e in errors), errors
            )

    def test_single_owner_is_not_a_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "dup_a.md", root / "dup_a.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("duplicate" in e.lower() for e in errors), errors)


class TestMemlintNotThisConcept(unittest.TestCase):
    def test_body_without_not_this_sentence_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "no_not_this.md", root / "no_not_this.md")
            _errors, warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-NO-NOT-THIS" in w and "not this" in w.lower() for w in warnings), warnings
            )

    def test_body_with_not_this_sentence_does_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            _errors, warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(
                any("CON-CODE-GOOD" in w and "not this" in w.lower() for w in warnings), warnings
            )


class TestMemlintExistingRulesStillGreen(unittest.TestCase):
    def test_v11_concept_fixtures_still_lint_clean_of_the_original_checks(self):
        v11_concepts = TOOLS_DIR / "fixtures" / "v11" / "concepts"
        v11_code = TOOLS_DIR / "fixtures" / "v11" / "code"
        errors, _warnings = memlint.lint_root(v11_concepts, code_root=v11_code)
        # CON-900's missing path is still expected; nothing NEW should
        # appear for CON-007 (media-identity) or CON-901 (no-tests).
        self.assertFalse(any("CON-007" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
