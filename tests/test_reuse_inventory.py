"""The reuse inventory (docs/REUSE-INVENTORY.json) and its checker (mc_reuse.py).

The production tree passes; for every guard method a fixture tree -- a COPY of
the production tree in a temp dir, never the real one -- with a second
recognisable implementation inserted fails; the legitimate wrappers, the
deliberate variants, documentation examples and test fixtures stay green."""
import copy
import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import mc_reuse  # noqa: E402

_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".worktrees", "__pycache__", "tests", "fixtures", "bench", "memory", ".venv", ".superpowers",
    "node_modules", "skills", "templates", ".github")


class FixtureTree:
    """A temp copy of the production tree plus edit helpers."""

    _base = None

    @classmethod
    def base(cls):
        if cls._base is None:
            cls._base = Path(tempfile.mkdtemp(prefix="reuse-base-"))
            shutil.copytree(TOOLS_DIR, cls._base / "t", ignore=_COPY_IGNORE)
        return cls._base / "t"

    def __init__(self, test):
        self.root = Path(tempfile.mkdtemp(prefix="reuse-fixture-")) / "t"
        test.addCleanup(shutil.rmtree, self.root.parent, True)
        shutil.copytree(self.base(), self.root)

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def append(self, rel, text):
        self.write(rel, self.read(rel) + text)

    def replace(self, rel, old, new, count=1):
        text = self.read(rel)
        assert old in text, f"{old!r} not in {rel}"
        self.write(rel, text.replace(old, new, count))

    def findings(self):
        return mc_reuse.check_inventory(self.root, check_tests=False)

    def inventory(self):
        return json.loads(self.read("docs/REUSE-INVENTORY.json"))

    def save_inventory(self, inv):
        self.write("docs/REUSE-INVENTORY.json", json.dumps(inv))


def only(findings, row_id, method=None):
    pre = f"{row_id}: " + (f"{method}: " if method else "")
    return [f for f in findings if f.startswith(pre)]


class TestProductionTree(unittest.TestCase):
    def test_unchanged_tree_is_clean(self):
        self.assertEqual(mc_reuse.check_inventory(TOOLS_DIR), [])

    def test_every_seed_job_is_present_and_every_singleton_names_its_methods(self):
        inv = mc_reuse.load_inventory(TOOLS_DIR / mc_reuse.INVENTORY_RELPATH)
        ids = {r["id"] for r in inv["rows"]}
        for want in ("join-semicolon-list", "coverage-fact-sentence", "coverage-question-sentence",
                     "filing-typed-authorities", "link-raw-spans", "anchor-identity", "shell-millisecond-clock",
                     "store-lint-schema", "store-lint-append-only", "record-text-parser",
                     "hold-eligible-authorities", "link-classification", "parser-adapters",
                     "chunker-declared-symbols", "chunker-chunk-file", "chunker-doc-extraction"):
            self.assertIn(want, ids)
        for r in inv["rows"]:
            self.assertTrue(r["coverage_limit"].strip(), r["id"])
            if r["policy"] == "singleton":
                self.assertTrue(r["guards"], r["id"])
            self.assertNotEqual(r["policy"], "known_debt", "the completed seed carries no known debt")

    def test_scan_covers_python_shell_and_embedded_payloads(self):
        inv = mc_reuse.load_inventory(TOOLS_DIR / mc_reuse.INVENTORY_RELPATH)
        scan = mc_reuse.scan_tree(TOOLS_DIR, inv["scan_roots"], inv["exclusions"])
        self.assertEqual(scan.problems, [])
        self.assertLess(scan.seconds, 30)
        langs = set(scan.files.values())
        self.assertEqual(langs, {"py", "sh"})
        self.assertTrue(any(d.embedded for d in scan.defs) or scan.trees)
        self.assertTrue(any(t[3] for t in scan.trees), "no embedded python payload was extracted")
        self.assertFalse(any(p.startswith("tests/") for p in scan.files))
        self.assertFalse(any(p.startswith(("docs/", "fixtures/", "bench/")) for p in scan.files))

    def test_cli_reports_clean(self):
        self.assertEqual(mc_reuse.main([str(TOOLS_DIR)]), 0)


class TestGuardsRejectSecondImplementation(unittest.TestCase):
    def test_same_name_shell_copy_fails_name_owner(self):
        ft = FixtureTree(self)
        ft.append("scripts/memcontinuum-update.sh",
                  '\njoin_semi() {\n    local out="" first=1 a\n    for a in "$@"; do\n'
                  '        if [ "$first" -eq 1 ]; then out="$a"; first=0; else out="$out;$a"; fi\n'
                  '    done\n    printf \'%s\' "$out"\n}\n')
        f = only(ft.findings(), "join-semicolon-list", "name_owner")
        self.assertEqual(len(f), 1, ft.findings())
        self.assertIn("memcontinuum-update.sh", f[0])

    def test_renamed_shell_copy_fails_exact_copy_but_not_the_name_guard(self):
        ft = FixtureTree(self)
        ft.append("scripts/repo-init.sh",
                  '\nglue_items() {\n    # my own joiner\n    local acc="" initial=1 item\n'
                  '    for item in "$@"; do\n'
                  '        if [ "$initial" -eq 1 ]; then acc="$item"; initial=0; else acc="$acc;$item"; fi\n'
                  '    done\n    printf \'%s\' "$acc"\n}\n')
        found = ft.findings()
        self.assertEqual(only(found, "join-semicolon-list", "name_owner"), [])
        f = only(found, "join-semicolon-list", "exact_copy")
        self.assertEqual(len(f), 1, found)
        self.assertIn("glue_items", f[0])

    def test_renamed_python_copy_fails_exact_copy(self):
        ft = FixtureTree(self)
        ft.append("memlint.py",
                  '\n\ndef ident_of_anchor(entry) -> "str | None":\n    """Same job, new name."""\n'
                  '    if not isinstance(entry, dict):\n        return None\n'
                  '    bits = [entry.get(k) for k in ("ref", "link", "captured", "captured_at")]\n'
                  '    if any(b is None or isinstance(b, (dict, list)) for b in bits):\n        return None\n'
                  '    return "\\x00".join(str(b) for b in bits)\n')
        found = ft.findings()
        self.assertEqual(only(found, "anchor-identity", "name_owner"), [])
        f = only(found, "anchor-identity", "exact_copy")
        self.assertEqual(len(f), 1, found)
        self.assertIn("ident_of_anchor", f[0])

    def test_python_exact_copy_keeps_literals_so_a_different_function_is_not_flagged(self):
        ft = FixtureTree(self)
        ft.append("memlint.py",
                  '\n\ndef other_ident(entry):\n    if not isinstance(entry, dict):\n        return None\n'
                  '    bits = [entry.get(k) for k in ("ref", "link")]\n'
                  '    if any(b is None or isinstance(b, (dict, list)) for b in bits):\n        return None\n'
                  '    return "\\x00".join(str(b) for b in bits)\n')
        self.assertEqual(only(ft.findings(), "anchor-identity"), [])

    def test_same_file_addition_fails(self):
        ft = FixtureTree(self)
        ft.append("memlint.py", "\n\ndef _link_raw_spans(text, parsed_links, key='links', ident=None):\n    return {}\n")
        f = [x for x in ft.findings() if x.startswith("link-raw-spans: ") and "defined 2 times" in x]
        self.assertEqual(len(f), 1, ft.findings())

    def test_new_production_python_file_with_the_name_fails(self):
        ft = FixtureTree(self)
        ft.write("newmod.py", "def parse_record_text(text, path):\n    return None\n")
        self.assertEqual(len(only(ft.findings(), "record-text-parser", "name_owner")), 1)

    def test_same_name_def_inside_match_and_async_with_in_a_new_file_fails(self):
        ft = FixtureTree(self)
        ft.write("newmod.py",
                 "async def run(mode, cm):\n"
                 "    match mode:\n"
                 "        case 1:\n"
                 "            def parse_record_text(text, path):\n"
                 "                return None\n"
                 "    async with cm:\n"
                 "        def _anchor_ident(row):\n"
                 "            return None\n")
        found = ft.findings()
        self.assertEqual(len(only(found, "record-text-parser", "name_owner")), 1, found)
        self.assertEqual(len(only(found, "anchor-identity", "name_owner")), 1, found)

    def test_new_production_file_with_a_coverage_sentence_fails_token_owner(self):
        ft = FixtureTree(self)
        ft.write("hooks/extra_nudge.py",
                 'def my_fact(n):\n    return f"Coverage signal — {n} edited file(s) with no decision topic"\n')
        f = only(ft.findings(), "coverage-fact-sentence", "token_owner")
        self.assertEqual(len(f), 2, f)   # one per fragment
        self.assertTrue(all("my_fact" in x for x in f))

    def test_embedded_python_heredoc_second_owner_fails(self):
        ft = FixtureTree(self)
        ft.write("hooks/another-hook.sh",
                 '#!/usr/bin/env bash\n"$MC_PY" - <<\'PYEOF\'\n'
                 'def ask(root):\n    return "Any ruling, incident, or rejected alternative from this session that"\n'
                 'PYEOF\n')
        f = only(ft.findings(), "coverage-question-sentence", "token_owner")
        self.assertEqual(len(f), 1, ft.findings())
        self.assertIn("another-hook.sh", f[0])

    def test_embedded_python_dash_c_payload_second_owner_fails(self):
        ft = FixtureTree(self)
        ft.write("hooks/another-hook.sh",
                 '#!/usr/bin/env bash\nout="$(env PYTHONPATH= "$MC_PY" -c \'\nimport sys\n'
                 'def fact():\n    return "Coverage signal"\nprint(fact())\n\')"\n')
        f = only(ft.findings(), "coverage-fact-sentence", "token_owner")
        self.assertEqual(len(f), 1, ft.findings())

    def test_embedded_python_name_copy_is_found(self):
        ft = FixtureTree(self)
        ft.write("scripts/odd.sh",
                 '#!/usr/bin/env bash\n"$PY" - <<EOF\ndef _anchor_ident(row):\n    return None\nEOF\n')
        self.assertEqual(len(only(ft.findings(), "anchor-identity", "name_owner")), 1)

    def test_unparseable_recognised_payload_is_a_scan_problem_naming_the_line(self):
        ft = FixtureTree(self)
        ft.write("hooks/odd-hook.sh",
                 '#!/usr/bin/env bash\necho start\n"$MC_PY" - <<\'PYEOF\'\nthis is ( not python\nPYEOF\n')
        found = ft.findings()
        f = [x for x in found if x.startswith("scan: hooks/odd-hook.sh:4:")]
        self.assertEqual(len(f), 1, found)
        self.assertIn("embedded python payload is not parseable", f[0])

    def test_unparseable_recognised_dash_c_payload_is_a_scan_problem(self):
        ft = FixtureTree(self)
        ft.write("hooks/odd-hook.sh", "#!/usr/bin/env bash\n\"$MC_PY\" -c 'def (:'\n")
        f = [x for x in ft.findings() if x.startswith("scan: hooks/odd-hook.sh:2:")]
        self.assertEqual(len(f), 1, ft.findings())

    def test_tab_indented_dash_heredoc_payload_defining_a_guarded_symbol_fails(self):
        ft = FixtureTree(self)
        ft.write("scripts/odd.sh",
                 '#!/usr/bin/env bash\n"$PY" - <<-\'EOF\'\n\tdef _anchor_ident(row):\n\t    return None\n\tEOF\n')
        found = ft.findings()
        self.assertEqual(len(only(found, "anchor-identity", "name_owner")), 1, found)
        self.assertEqual([x for x in found if x.startswith("scan:")], [])

    def test_unterminated_heredoc_is_reported_with_the_line_of_its_opener(self):
        ft = FixtureTree(self)
        ft.write("hooks/odd-hook.sh",
                 '#!/usr/bin/env bash\necho a\ncat <<\'NEVER\'\ndata\nmore data\n')
        found = ft.findings()
        f = [x for x in found if x.startswith("scan: hooks/odd-hook.sh:3:")]
        self.assertEqual(len(f), 1, found)
        self.assertIn("unterminated heredoc", f[0])

    def test_direct_lint_call_bypassing_the_delegation_fails(self):
        ft = FixtureTree(self)
        ft.append("hooks/pre-commit-append-only.sh",
                  '\nPYTHONPATH= "$PY" "$MEMLINT" --against-ref HEAD --staged "$MEMCONTINUUM_ROOT"\n')
        found = ft.findings()
        d = only(found, "store-lint-append-only", "delegation")
        self.assertTrue(any("bypassing the owner" in x for x in d), found)
        t = only(found, "store-lint-append-only", "token_owner")
        self.assertTrue(any("another owner" in x for x in t), found)

    def test_consumer_that_stops_calling_the_owner_fails(self):
        ft = FixtureTree(self)
        ft.replace("hooks/pre-commit-append-only.sh", "mc_store_lint_schema ", "true # ")
        f = only(ft.findings(), "store-lint-schema", "delegation")
        self.assertEqual(len(f), 1, ft.findings())
        self.assertIn("does not reach the job through the owner", f[0])

    def test_elapsed_timing_hook_with_its_own_clock_fails(self):
        ft = FixtureTree(self)
        ft.append("hooks/pre-edit-chain.sh", '\nstart_ms="${EPOCHREALTIME/[.,]/}"\n')
        f = only(ft.findings(), "shell-millisecond-clock", "delegation")
        self.assertTrue(any("bypassing" in x for x in f), ft.findings())

    def test_memlint_invoked_without_pythonpath_from_a_commit_hook_fails_both_lint_rows(self):
        for line in ('"$PY" "$MEMLINT" "$MEMCONTINUUM_ROOT"', 'python3 "$SCRIPT_DIR/../memlint.py" "$MEMCONTINUUM_ROOT"',
                     'out="$(env X=1 "$PY" "$memlint" --against-ref HEAD "$ROOT")"'):
            ft = FixtureTree(self)
            ft.append("hooks/pre-commit-append-only.sh", "\n" + line + "\n")
            found = ft.findings()
            for rid in ("store-lint-schema", "store-lint-append-only"):
                d = only(found, rid, "delegation")
                self.assertTrue(any("bypassing the owner" in x for x in d), (line, rid, found))

    def test_direct_lint_forms_the_round_2_gate_found_fail_both_lint_rows(self):
        # each of these ran the lint inside a commit hook and left the complete check clean
        for line in ('"$memlint" "$MEMCONTINUUM_ROOT"',
                     '"$SCRIPT_DIR/../memlint.py" "$MEMCONTINUUM_ROOT"',
                     'X=1 "$PY" "$memlint" "$MEMCONTINUUM_ROOT"',
                     'PYTHONPATH= python3 "$memlint" "$MEMCONTINUUM_ROOT"',
                     '"${MEMLINT}" --against-ref HEAD "$ROOT"',
                     'out=$("$MC_PY" -u "$MEMLINT" "$ROOT")'):
            ft = FixtureTree(self)
            ft.append("hooks/post-commit-reindex.sh", "\n" + line + "\n")
            found = ft.findings()
            for rid in ("store-lint-schema", "store-lint-append-only"):
                d = only(found, rid, "delegation")
                self.assertTrue(any("bypassing the owner" in x for x in d), (line, rid, found))

    def test_text_that_merely_mentions_the_lint_is_not_a_direct_call(self):
        for line in ("echo 'python3 memlint.py'", 'echo "$PY" "$MEMLINT" "$ROOT"', 'ls "$SCRIPT_DIR/../memlint.py"',
                     'MEMLINT="$SCRIPT_DIR/../memlint.py"', 'mc_store_lint_schema "$PY" "$MEMLINT" "$ROOT"',
                     'mc_store_lint_append_only "$PY" "$memlint" "$ROOT" HEAD'):
            ft = FixtureTree(self)
            ft.append("hooks/post-commit-reindex.sh", "\n" + line + "\n")
            self.assertEqual(ft.findings(), [], line)

    def test_a_millisecond_date_named_only_as_text_is_not_a_clock(self):
        ft = FixtureTree(self)
        ft.append("hooks/newfile-nudge.sh", "\necho 'date +%s%3N'\n")
        self.assertEqual(ft.findings(), [])

    def test_passing_memlint_to_the_owner_is_not_an_invocation(self):
        ft = FixtureTree(self)
        ft.append("hooks/post-commit-reindex.sh",
                  '\nmc_store_lint_schema "$PY" "$memlint" "$ROOT"\nMEMLINT="$SCRIPT_DIR/../memlint.py"\n')
        self.assertEqual(ft.findings(), [])

    def test_elapsed_timing_hooks_cannot_grow_a_millisecond_clock_of_their_own(self):
        for line in ('t0=$(( $(date +%s) * 1000 ))', 't0="$(date +%s)000"', 'ms=$(date +%s%3N)',
                     't0=$(python3 -c "import time; print(time.time_ns())")',
                     't0=$(python3 -c "import time; print(int(time.time() * 1000))")'):
            ft = FixtureTree(self)
            ft.append("hooks/newfile-nudge.sh", "\n" + line + "\n")
            f = only(ft.findings(), "shell-millisecond-clock", "delegation")
            self.assertTrue(any("bypassing" in x for x in f), (line, ft.findings()))

    def test_a_whole_second_date_read_in_an_elapsed_timing_hook_is_not_forbidden(self):
        ft = FixtureTree(self)
        ft.append("hooks/newfile-nudge.sh", '\nSTAMP="$(date +%s 2>/dev/null || echo 0)"\n')
        self.assertEqual(ft.findings(), [])

    def test_second_perl_clock_fails_token_owner(self):
        ft = FixtureTree(self)
        ft.write("hooks/clock2.sh",
                 "#!/usr/bin/env bash\nnow2() {\n    perl -MTime::HiRes=time -e 'print time()'\n}\n")
        self.assertEqual(len(only(ft.findings(), "shell-millisecond-clock", "token_owner")), 1)

    def test_python_consumer_bypassing_via_direct_yaml_fails(self):
        ft = FixtureTree(self)
        ft.append("memlint.py", "\n\ndef _raw_load(text):\n    import yaml\n    return yaml.safe_load(text)\n")
        f = only(ft.findings(), "record-text-parser", "delegation")
        self.assertTrue(any("bypassing" in x for x in f), ft.findings())

    def test_promotion_with_its_own_vocabulary_fails_both_guards(self):
        ft = FixtureTree(self)
        ft.replace("memidx.py", "return authority in FILING_TYPED_AUTHORITIES and status in PROMOTABLE_STATUSES",
                   'return authority in ("agent-inference", "reviewer-finding", "code-derived") '
                   "and status in PROMOTABLE_STATUSES")
        found = ft.findings()
        self.assertEqual(len(only(found, "filing-typed-authorities", "vocab_owner")), 1, found)
        self.assertEqual(len(only(found, "filing-typed-authorities", "delegation")), 1, found)

    def test_vocab_literal_in_an_embedded_payload_fails(self):
        ft = FixtureTree(self)
        ft.write("hooks/vocab-hook.sh",
                 '#!/usr/bin/env bash\n"$MC_PY" -c \'\nOK = {"code-derived", "agent-inference", "reviewer-finding"}\n\'\n')
        f = only(ft.findings(), "filing-typed-authorities", "vocab_owner")
        self.assertEqual(len(f), 1, ft.findings())
        self.assertIn("embedded python", f[0])

    def test_frozen_helper_moved_or_removed_is_reported_not_ignored(self):
        ft = FixtureTree(self)
        ft.replace("memlint.py", "def _anchor_ident(row)", "def _anchor_identity(row)")
        found = ft.findings()
        self.assertTrue(any(f.startswith("anchor-identity: ") and "symbol not found" in f for f in found), found)


def _deleg_row(consumers, owner="a.py#f", refs=None, **extra):
    params = {"consumers": consumers}
    if refs is not None:
        params["refs"] = refs
    params.update(extra)
    return _row(implementation=owner, guards=[{"method": "delegation", "params": params}])


class TestDelegationNeedsAnExecutableCall(unittest.TestCase):
    """A reference counts where it is executed, not where it is mentioned."""

    def check(self, files, row):
        root = _mini(self, files)
        return mc_reuse.check_inventory(root, _inv(row), check_tests=False)

    def test_post_commit_keeping_the_type_probe_but_inlining_the_lint_call_fails(self):
        ft = FixtureTree(self)
        ft.replace("hooks/post-commit-reindex.sh",
                   '    mc_store_lint_schema "$PY" "$memlint" "$MEMCONTINUUM_ROOT"\n',
                   '    "$PY" "$memlint" "$MEMCONTINUUM_ROOT"\n')
        self.assertIn("type mc_store_lint_schema", ft.read("hooks/post-commit-reindex.sh"))
        f = [x for x in only(ft.findings(), "store-lint-schema", "delegation")
             if "does not reach the job through the owner" in x]
        self.assertEqual(len(f), 1, ft.findings())
        self.assertIn("post-commit-reindex.sh", f[0])

    def test_post_commit_with_a_redirect_operand_named_like_the_owner_and_an_assigned_lint_call_fails(self):
        # the round-2 gate's escape, verbatim: `> mc_store_lint_schema` is a redirection target, not a call, and the
        # assignment prefix hid the inline lint call from the old forbid
        ft = FixtureTree(self)
        ft.replace("hooks/post-commit-reindex.sh",
                   '    mc_store_lint_schema "$PY" "$memlint" "$MEMCONTINUUM_ROOT"\n',
                   '    > mc_store_lint_schema\n    X=1 "$PY" "$memlint" "$MEMCONTINUUM_ROOT"\n')
        f = [x for x in only(ft.findings(), "store-lint-schema", "delegation")
             if "does not reach the job through the owner" in x]
        self.assertEqual(len(f), 1, ft.findings())
        self.assertIn("post-commit-reindex.sh", f[0])

    def test_python_consumer_that_imports_but_never_calls_fails(self):
        f = self.check({"a.py": "def f(x):\n    return x\n",
                        "c.py": "from a import f\n\ndef parse(text):\n    return text.split()\n"},
                       _deleg_row(["c.py"]))
        self.assertEqual(len(f), 1, f)
        self.assertIn("no executable call", f[0])

    def test_python_name_loaded_but_not_called_fails_for_a_function_owner(self):
        f = self.check({"a.py": "def f(x):\n    return x\n",
                        "c.py": "from a import f\n\nHANDLERS = [f]\n\ndef parse(text):\n    return text\n"},
                       _deleg_row(["c.py"]))
        self.assertEqual(len(f), 1, f)

    def test_python_call_and_attribute_call_both_count(self):
        files = {"a.py": "def f(x):\n    return x\n",
                 "c.py": "import a\n\ndef parse(t):\n    return a.f(t)\n",
                 "d.py": "from a import f\n\ndef parse(t):\n    return f(t)\n"}
        self.assertEqual(self.check(files, _deleg_row(["c.py", "d.py"])), [])

    def test_constant_owner_is_reached_by_a_value_reference(self):
        files = {"a.py": "K = ('x', 'y')\n", "c.py": "from a import K\n\ndef ok(v):\n    return v in K\n",
                 "d.py": "from a import K\n\ndef no(v):\n    return v\n"}
        row = _deleg_row(["c.py"], owner="a.py#K")
        self.assertEqual(self.check(files, row), [])
        f = self.check(files, _deleg_row(["d.py"], owner="a.py#K"))
        self.assertEqual(len(f), 1, f)
        self.assertIn("no executable reference", f[0])

    def test_an_attribute_store_is_not_a_reference_to_a_constant_owner(self):
        files = {"a.py": "K = ('x', 'y')\n",
                 "s.py": "import a\n\nclass O:\n    pass\n\ndef set_it(obj):\n    obj.K = 1\n    obj.K += 1\n    del obj.K\n",
                 "l.py": "import a\n\ndef get_it(obj):\n    return obj.K\n"}
        f = self.check(files, _deleg_row(["s.py"], owner="a.py#K"))
        self.assertEqual(len(f), 1, f)
        self.assertIn("no executable reference", f[0])
        self.assertEqual(self.check(files, _deleg_row(["l.py"], owner="a.py#K")), [])

    def test_post_commit_whose_owner_call_became_a_non_command_parenthesis_fails_with_only_that_finding(self):
        # the round-3 gate's escapes: each leaves a mention of the owner where its call was, and none runs it
        for repl in ("[[ (mc_store_lint_schema) ]]",
                     "for ((mc_store_lint_schema;0;0)); do :; done",
                     "case x in @(a)mc_store_lint_schema) :;; esac",
                     # an escaped double quote does not end the header's string early (round-4 gate)
                     'for ((i="\\"))";x=";mc_store_lint_schema;";0)); do :; done'):
            ft = FixtureTree(self)
            ft.replace("hooks/post-commit-reindex.sh",
                       '    mc_store_lint_schema "$PY" "$memlint" "$MEMCONTINUUM_ROOT"\n', "    " + repl + "\n")
            found = ft.findings()
            self.assertEqual(len(found), 1, (repl, found))
            self.assertIn("store-lint-schema: delegation:", found[0])
            self.assertIn("does not reach the job through the owner", found[0])
            self.assertIn("post-commit-reindex.sh", found[0])

    def test_skipped_regions_read_quotes_and_escapes_as_ordinary_words_do(self):
        words = mc_reuse._sh_command_words
        self.assertNotIn("mc_job", words('for ((i="\\"))";x=";mc_job;";0)); do :; done\n'))
        self.assertEqual(words('arr=("\\")" mc_job)\n'), set())
        self.assertEqual(words('a=(x $\'\\\')\' y); mc_job\n'), {"mc_job"})
        self.assertEqual(words('x=${a:-"}"}; mc_job\n'), {"mc_job"})
        self.assertEqual(words('[[ "\\"]]" ]]; mc_job\n'), {"mc_job"})
        self.assertEqual(words('((x="a\\"))"));  mc_job\n'), {"mc_job"})

    def test_a_lint_call_that_ends_in_a_closing_backtick_is_a_direct_call(self):
        for line in ('out=`"$PY" "$memlint"`', '`python3 "$memlint"`', '`"$memlint"`', 'out=`"$PY" "$memlint" "$ROOT"`'):
            ft = FixtureTree(self)
            ft.append("hooks/post-commit-reindex.sh", "\n" + line + "\n")
            found = ft.findings()
            for rid in ("store-lint-schema", "store-lint-append-only"):
                d = only(found, rid, "delegation")
                self.assertTrue(any("bypassing the owner" in x for x in d), (line, rid, found))

    def test_a_continued_assignment_prefix_makes_an_array_literal_that_renders_no_command(self):
        for src in ("foo=\\\n(command python3 memlint.py)\n", "foo+=\\\n(command python3 memlint.py)\n",
                    "a[0]=\\\n(command python3 memlint.py)\n"):
            self.assertEqual(mc_reuse._sh_command_words(src), set(), src)
            self.assertEqual(mc_reuse._sh_command_strings(src), (), src)
        # a real newline ends the assignment, so the parentheses are a subshell that bash runs
        self.assertEqual(mc_reuse._sh_command_words("foo=\n(mc_job)\n"), {"mc_job"})

    def test_a_string_inside_a_conditional_is_not_a_direct_lint_call(self):
        ft = FixtureTree(self)
        ft.append("hooks/post-commit-reindex.sh", '\n[[ ("python3 memlint.py") ]]\n')
        self.assertEqual(ft.findings(), [])

    def test_calls_inside_a_conditional_branch_and_a_subshell_still_count(self):
        owner = "a.sh#mc_job"
        base = {"a.sh": "mc_job() {\n    echo hi\n}\n"}
        for name, body in {"if-double-bracket": 'if [[ -n "$x" ]]; then mc_job; fi\n',
                           "subshell": "( mc_job )\n",
                           "after-conditional": "[[ -n $x ]] && mc_job\n",
                           "char-class-in-conditional": "[[ a == [[:alpha:]] ]] && mc_job\n",
                           "for-body": "for ((i=0;i<3;i++)); do mc_job; done\n",
                           "case-after-extglob": "case x in @(a|b(c))) mc_job;; esac\n"}.items():
            files = dict(base, **{"c.sh": "#!/usr/bin/env bash\n" + body})
            self.assertEqual(self.check(files, _deleg_row(["c.sh"], owner=owner)), [], name)

    def test_shell_calls_through_a_pipe_a_substitution_and_an_assignment_are_clean(self):
        owner = "a.sh#mc_job"
        base = {"a.sh": "mc_job() {\n    echo hi\n}\n"}
        shapes = {
            "pipe": "echo x | mc_job\n",
            "subst": 'out="$(mc_job "$1")"\n',
            "backtick": "out=`mc_job a`\n",
            "and": "true && mc_job\n",
            "assign-prefix": "FOO=1 BAR=2 mc_job a\n",
            "if": "if mc_job; then echo ok; fi\n",
            "negated": "if ! mc_job; then echo no; fi\n",
            "brace": "{ mc_job; }\n",
            "time": "time mc_job\n",
            "inline-subst": 'echo "got $(mc_job)"\n',
            "double-quoted": '"mc_job" a\n',
            "single-quoted-word": "'mc_job' a\n",
            "backtick-bare": "`mc_job`\n",
            "backtick-assigned": "out=`mc_job`\n",
            "for-body-after-arithmetic-header": "for ((i=0; i<3; i++)); do mc_job; done\n",
            "continued-command-word": "mc_job\\\n  arg\n",
            "continued-command-word-quoted": '"mc_job"\\\n  arg\n',
            "assignment-then-a-real-newline-then-a-subshell": "foo=\n(mc_job)\n",
            "continuation-inside-a-double-quoted-name": '"mc_\\\njob" arg\n',
            "eval-plain": "eval mc_job\n",
            "eval-double-quoted": 'eval "mc_job"\n',
            "eval-single-quoted": "eval 'mc_job'\n",
            "redirect-before-the-command": ">out mc_job\n",
            "redirect-fd-before-the-command": "2>&1 mc_job\n",
            "case-body": 'case $x in\n    a)\n        mc_job\n        ;;\nesac\n',
            "nested-case-body": 'case $x in a) case $y in b) mc_job;; esac;; esac\n',
        }
        for name, body in shapes.items():
            files = dict(base, **{"c.sh": "#!/usr/bin/env bash\n" + body})
            self.assertEqual(self.check(files, _deleg_row(["c.sh"], owner=owner)), [], name)

    def test_shell_mentions_are_not_calls(self):
        owner = "a.sh#mc_job"
        base = {"a.sh": "mc_job() {\n    echo hi\n}\n"}
        shapes = {
            "commented": "# mc_job\n",
            "trailing-comment": "echo x # mc_job\n",
            "type-probe": "type mc_job >/dev/null 2>&1\n",
            "declare-f": "declare -f mc_job >/dev/null\n",
            "command-v": "command -v mc_job >/dev/null\n",
            "hash": "hash mc_job\n",
            "unset": "unset -f mc_job\n",
            "single-quoted": "echo 'run mc_job | mc_job'\n",
            "argument": "echo mc_job\n",
            "declaration": "mc_job() {\n    echo mine\n}\n",
            "redirect-operand": "> mc_job\n",
            "redirect-operand-append": "echo x >> mc_job\n",
            "redirect-operand-both": "echo x &> mc_job\n",
            "redirect-operand-input": "cat < mc_job\n",
            "case-pattern": 'case $x in\n    mc_job)\n        echo hi\n        ;;\nesac\n',
            "case-pattern-alternation": 'case $x in (a|mc_job) echo hi;; esac\n',
            "arithmetic-command": "((mc_job))\n",
            "subscript-array-literal": "a[0]=(mc_job)\n",
            "subscript-array-append": "arr[i]+=(mc_job other)\n",
            "local-subscript-array": "f() {\n    local a[0]=(mc_job)\n}\n",
            "for-arithmetic-header-with-redirection-shape": "for ((i=0; mc_job<0; i++)); do :; done\n",
            "for-arithmetic-header-empty-init": "for ((;mc_job<0;)); do :; done\n",
            "for-variable-named-like-the-owner": "for mc_job in a b; do echo x; done\n",
            "double-bracket-group": "[[ (mc_job) ]]\n",
            "double-bracket-substitution": "[[ $(mc_job) == x ]]\n",
            "for-arithmetic-header": "for ((mc_job;0;0)); do :; done\n",
            "case-extglob-then-word": "case x in @(a)mc_job) :;; esac\n",
            "single-bracket-group": "[ (mc_job) ]\n",
            "array-literal": "arr=(mc_job)\n",
            "continued-assignment-array-literal": "foo=\\\n(mc_job)\n",
            "continued-append-array-literal": "foo+=\\\n(mc_job)\n",
            "continued-subscript-array-literal": "a[0]=\\\n(mc_job)\n",
            "array-append": "arr+=(mc_job other)\n",
            "local-array": "f() {\n    local arr=(mc_job)\n}\n",
            "longer-word-quoted-suffix": 'mc_job"other"\n',
            "longer-word-variable-suffix": "mc_job$x\n",
            "longer-word-path": "mc_job/x\n",
            "continued-into-a-longer-word": "mc_job\\\nother arg\n",
            "continuation-kept-inside-single-quotes-after-the-name": "'mc_job\\\n' arg\n",
            "continuation-kept-inside-single-quotes-in-the-name": "'mc_\\\njob' arg\n",
            "eval-complex-word": 'eval "mc_job x"\n',
            "launcher": "xargs mc_job\n",
            "command-p": "command -p mc_job\n",
        }
        for name, body in shapes.items():
            files = dict(base, **{"c.sh": "#!/usr/bin/env bash\n" + body})
            f = self.check(files, _deleg_row(["c.sh"], owner=owner))
            self.assertTrue(any("does not reach the job" in x for x in f), (name, f))

    def test_a_commented_out_call_alone_fails_in_the_production_hook(self):
        ft = FixtureTree(self)
        ft.replace("hooks/pre-commit-append-only.sh",
                   'mc_store_lint_schema "$PY" "$MEMLINT" "$MEMCONTINUUM_ROOT"',
                   '# mc_store_lint_schema "$PY" "$MEMLINT" "$MEMCONTINUUM_ROOT"')
        f = only(ft.findings(), "store-lint-schema", "delegation")
        self.assertEqual(len(f), 1, ft.findings())
        self.assertIn("pre-commit-append-only.sh", f[0])


class TestPositiveControls(unittest.TestCase):
    def test_legitimate_wrapper_that_calls_the_owner_stays_green(self):
        ft = FixtureTree(self)
        ft.append("scripts/repo-init.sh",
                  '\nquote_join() {\n    local enc=() a\n    for a in "$@"; do enc+=("\'$a\'"); done\n'
                  '    mc_join_semi "${enc[@]}"\n}\n')
        self.assertEqual(ft.findings(), [])

    def test_documentation_examples_and_test_fixtures_are_outside_the_scan(self):
        ft = FixtureTree(self)
        ft.write("docs/example.sh", "join_semi() {\n    echo hi\n}\n")
        ft.write("docs/example.py", "def parse_record_text(t, p):\n    return t\n")
        ft.write("tests/fixture_tool.py", "def _link_raw_spans():\n    pass\n")
        ft.write("fixtures/x/hook.sh", "mc_store_lint_append_only() {\n    echo --against-ref\n}\n")
        ft.write("bench/b.py", "COVERAGE = 'Coverage signal'\n")
        self.assertEqual(ft.findings(), [])

    def test_comments_and_strings_outside_executable_code_do_not_count(self):
        ft = FixtureTree(self)
        ft.append("hooks/pre-edit-chain.sh", "\n# see mc_store_lint_append_only and --against-ref, Coverage signal\n")
        ft.write("hooks/notes.sh", "#!/usr/bin/env bash\necho ok  # --against-ref perl -MTime::HiRes\n")
        ft.write("lintdoc.py", '"""Mentions --against-ref and Coverage signal in a docstring only."""\n')
        self.assertEqual(ft.findings(), [])

    def test_nested_docstrings_are_not_executable_text(self):
        ft = FixtureTree(self)
        ft.write("nested_doc.py",
                 'def outer(x):\n    """Outer prose."""\n\n    def inner(y):\n'
                 '        """Coverage signal is explained here; edited file(s) with no decision topic."""\n'
                 '        return y\n\n    class Helper:\n        """rejected alternative from this session"""\n\n'
                 '        def m(self):\n            """Coverage signal again."""\n            return 1\n'
                 '    return inner(x)\n')
        self.assertEqual(ft.findings(), [])
        # a string that is not a docstring still counts
        ft.write("nested_doc.py", 'def outer(x):\n    def inner(y):\n        return "Coverage signal"\n    return inner\n')
        self.assertEqual(len(only(ft.findings(), "coverage-fact-sentence", "token_owner")), 1, ft.findings())

    def test_a_bare_string_after_a_definition_is_code_not_a_docstring(self):
        ft = FixtureTree(self)
        ft.write("tail_string.py", 'def f():\n    pass\n\n"Coverage signal is explained here"\n')
        self.assertEqual(len(only(ft.findings(), "coverage-fact-sentence", "token_owner")), 1, ft.findings())
        # the genuine module docstring, and a class docstring, are still prose
        ft.write("tail_string.py", '"""Coverage signal is explained here."""\n\ndef f():\n    pass\n\n'
                                   'class C:\n    """Coverage signal again."""\n\n    x = 1\n')
        self.assertEqual(ft.findings(), [])
        s = mc_reuse.scan_tree(ft.root, ["."], ["tests"])
        unit = next(u for u in s.units if u.path == "tail_string.py" and u.qual == "<module>")
        self.assertNotIn("Coverage", unit.text)
        self.assertIn("x = 1", unit.text)

    def test_against_ref_outside_hooks_is_legitimate(self):
        ft = FixtureTree(self)
        ft.write("scripts/adapter.sh",
                 "#!/usr/bin/env bash\nrun_lint() {\n    python3 memlint.py --against-ref \"$1\"\n}\n")
        ft.write("adapter.py", "def args():\n    return ['--against-ref', 'HEAD']\n")
        self.assertEqual(ft.findings(), [])

    def test_unrelated_yaml_loading_is_not_prohibited(self):
        ft = FixtureTree(self)
        ft.write("loader.py", "import yaml\n\ndef load(text):\n    return yaml.safe_load(text)\n")
        self.assertEqual(ft.findings(), [])

    def test_unrelated_authority_like_sets_are_not_flagged(self):
        ft = FixtureTree(self)
        ft.write("vocab.py", 'A = ("agent-inference", "reviewer-finding")\nB = {"x", "y", "z"}\n')
        self.assertEqual(ft.findings(), [])

    def test_classifier_pair_and_hold_vocabulary_are_recorded_variants(self):
        inv = mc_reuse.load_inventory(TOOLS_DIR / mc_reuse.INVENTORY_RELPATH)
        rows = {r["id"]: r for r in inv["rows"]}
        lc = rows["link-classification"]
        self.assertEqual(lc["policy"], "allowed_variants")
        self.assertEqual({v["symbol"] for v in lc["variants"]}, {"memlint.py#_link_tier"})
        self.assertIn("invariant", lc["variants"][0]["reason"])
        self.assertIn("evidence", lc["variants"][0]["reason"])
        hv = rows["hold-eligible-authorities"]
        self.assertEqual(hv["policy"], "allowed_variants")
        self.assertIn("memidx.py#HOLD_ELIGIBLE_AUTHORITIES", {v["symbol"] for v in rows["filing-typed-authorities"]["variants"]})
        self.assertEqual(mc_reuse.check_inventory(TOOLS_DIR), [])

    def test_row_texts_state_the_one_token_miss_and_not_the_retired_claims(self):
        inv = mc_reuse.load_inventory(TOOLS_DIR / mc_reuse.INVENTORY_RELPATH)
        for r in inv["rows"]:
            text = r["coverage_limit"]
            if any(g["method"] == "exact_copy" for g in r["guards"]):
                self.assertIn("differs by one token", text, r["id"])
                self.assertIn("another name needs a human", text, r["id"])
            self.assertNotIn("structural rewrite", text, r["id"])
            self.assertNotIn("inside a double-quoted -c payload are not seen", text, r["id"])
        rows = {r["id"]: r for r in inv["rows"]}
        sentence = (r"(?:Not recognised: |is not recognised; )a copy whose normalised body differs by one token "
                    r"\(a literal, an added statement, reordered locals\) escapes; a rewrite under another name "
                    r"needs a human")
        for r in inv["rows"]:
            if any(g["method"] == "exact_copy" for g in r["guards"]):
                self.assertRegex(r["coverage_limit"], sentence, r["id"])
                self.assertNotIn("guard misses a copy", r["coverage_limit"], r["id"])
        for rid in ("coverage-fact-sentence", "coverage-question-sentence"):
            self.assertIn("non-Python heredoc", rows[rid]["coverage_limit"])
            self.assertIn("double-quoted -c payload", rows[rid]["coverage_limit"])
            self.assertIn("is seen", rows[rid]["coverage_limit"])
        for rid in ("store-lint-schema", "store-lint-append-only", "shell-millisecond-clock"):
            for miss in ("`xargs`", "`find -exec`", "`sudo`", "`env`", "`timeout`", "`coproc`", "`command -p`",
                         "passed to another by name", "nested inside `$((...))`", "one quoted word"):
                self.assertIn(miss, rows[rid]["coverage_limit"], (rid, miss))
        for rid in ("store-lint-schema", "store-lint-append-only", "shell-millisecond-clock"):
            self.assertIn("a command word continued over a backslash-newline is seen", rows[rid]["coverage_limit"], rid)
            self.assertIn("a `for ((...))` header, an extglob group or any parenthesis that is not a subshell is "
                          "skipped whole and NOT seen", rows[rid]["coverage_limit"], rid)
            self.assertIn("`[[ ]]`", rows[rid]["coverage_limit"], rid)
        for rid in ("store-lint-schema", "store-lint-append-only"):
            self.assertIn("the forbid sees it; the reach check does not", rows[rid]["coverage_limit"], rid)
        internals = (TOOLS_DIR / "docs" / "INTERNALS.md").read_text(encoding="utf-8")
        self.assertIn("so a call written inside them is NOT seen", internals)
        self.assertIn("A word continued over a backslash-newline", internals)
        self.assertIn("single- or double-quoted piece", internals)
        self.assertNotIn("single-quoted text or a longer word", internals)
        self.assertIn("inlined lint call that keeps the probe is caught", rows["store-lint-schema"]["coverage_limit"])
        self.assertIn("whole-second", rows["shell-millisecond-clock"]["coverage_limit"])

    def test_the_three_chunker_backend_families_stay_green_with_their_members_listed(self):
        inv = mc_reuse.load_inventory(TOOLS_DIR / mc_reuse.INVENTORY_RELPATH)
        rows = {r["id"]: r for r in inv["rows"]}
        for rid, count in (("chunker-declared-symbols", 3), ("chunker-chunk-file", 3), ("chunker-doc-extraction", 3)):
            self.assertEqual(rows[rid]["policy"], "allowed_variants")
            self.assertEqual(len(rows[rid]["variants"]), count)
            self.assertEqual(rows[rid]["guards"], [])
        self.assertEqual(mc_reuse.check_inventory(TOOLS_DIR), [])

    def test_parser_adapters_are_not_second_parser_cores(self):
        ft = FixtureTree(self)
        ft.append("memidx.py", "\n\ndef parse_record_bytes(raw, path):\n    return parse_record_raw(raw.decode(), path)\n")
        self.assertEqual(ft.findings(), [])

    def test_a_tiny_unrelated_function_is_not_an_exact_copy(self):
        ft = FixtureTree(self)
        ft.write("tiny.py", "def f(x):\n    return x\n")
        ft.write("scripts/tiny.sh", "tiny_fn() {\n    echo hi\n}\n")
        self.assertEqual(ft.findings(), [])


def _mini(test, files, inv_mutator=None):
    root = Path(tempfile.mkdtemp(prefix="reuse-mini-"))
    test.addCleanup(shutil.rmtree, root, True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def _row(**over):
    base = {"id": "r1", "job": "j", "policy": "singleton", "implementation": "a.py#f", "contract": "c",
            "why_single": "w", "related_paths": [], "variants": [], "guards": [{"method": "name_owner", "params": {}}],
            "coverage_limit": "l", "evidence": "e", "test": "tests.test_reuse_inventory.TestSchema"}
    base.update(over)
    return base


def _inv(*rows, **over):
    inv = {"schema_version": 1, "scan_roots": ["."], "exclusions": ["tests"], "tests_dir": None,
           "rows": list(rows)}
    inv.update(over)
    return inv


class TestSchema(unittest.TestCase):
    def errors(self, inv):
        return mc_reuse.validate_schema(inv)

    def test_forbid_entries_are_a_bare_string_or_one_scoped_pattern(self):
        def forbid(*entries):
            return _inv(_row(guards=[{"method": "delegation", "params": {"consumers": ["c.sh"], "forbid": list(entries)}}]))
        self.assertEqual(self.errors(forbid("x", {"text": "y"}, {"command": "^z"})), [])
        for bad in ({"shell": "x"}, {"text": "x", "command": "y"}, {"command": 7}, 7, {}):
            errs = self.errors(forbid(bad))
            self.assertTrue(any("forbid entry" in e for e in errs), (bad, errs))
        errs = self.errors(forbid({"command": "("}))
        self.assertTrue(any("not a valid regular expression" in e for e in errs), errs)

    def test_a_valid_minimal_inventory_has_no_errors(self):
        self.assertEqual(self.errors(_inv(_row())), [])

    def test_missing_top_level_field(self):
        inv = _inv(_row())
        del inv["exclusions"]
        self.assertTrue(any("missing top-level field 'exclusions'" in e for e in self.errors(inv)))

    def test_unknown_top_level_and_row_fields(self):
        inv = _inv(_row(extra=1), junk=1)
        errs = self.errors(inv)
        self.assertTrue(any("unknown top-level field 'junk'" in e for e in errs), errs)
        self.assertTrue(any("unknown field 'extra'" in e for e in errs), errs)

    def test_duplicate_ids(self):
        errs = self.errors(_inv(_row(), _row()))
        self.assertTrue(any("duplicate id" in e for e in errs), errs)

    def test_unknown_method(self):
        errs = self.errors(_inv(_row(guards=[{"method": "similarity", "params": {}}])))
        self.assertTrue(any("unknown method 'similarity'" in e for e in errs), errs)

    def test_malformed_exception_variant(self):
        errs = self.errors(_inv(_row(variants=[{"symbol": "a.py#g", "contract": "c"}])))
        self.assertTrue(any("variants[0]" in e for e in errs), errs)

    def test_bad_policy_and_unguarded_singleton(self):
        errs = self.errors(_inv(_row(policy="whatever")))
        self.assertTrue(any("policy must be one of" in e for e in errs), errs)
        errs = self.errors(_inv(_row(guards=[])))
        self.assertTrue(any("singleton row must name at least one guard" in e for e in errs), errs)

    def test_guard_parameter_errors(self):
        errs = self.errors(_inv(_row(guards=[{"method": "token_owner", "params": {}}])))
        self.assertTrue(any("needs a pattern" in e for e in errs), errs)
        errs = self.errors(_inv(_row(guards=[{"method": "name_owner", "params": {"pattern": "("}}])))
        self.assertTrue(any("not a valid regular expression" in e for e in errs), errs)
        errs = self.errors(_inv(_row(guards=[{"method": "delegation", "params": {"consumers": []}}])))
        self.assertTrue(any("consumers" in e for e in errs), errs)

    def test_known_debt_must_name_implementations_and_cannot_be_guarded(self):
        good = _row(policy="known_debt", guards=[],
                    variants=[{"symbol": "a.py#g", "contract": "c", "reason": "merge into f"}])
        self.assertEqual(self.errors(_inv(good)), [])
        errs = self.errors(_inv(_row(policy="known_debt", guards=[], variants=[])))
        self.assertTrue(any("must name every current implementation" in e for e in errs), errs)
        errs = self.errors(_inv(copy.deepcopy(good) | {"guards": [{"method": "name_owner", "params": {}}]}))
        self.assertTrue(any("never guarded" in e for e in errs), errs)

    def test_bad_json_and_non_object(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "x.json").write_text("{nope", encoding="utf-8")
        with self.assertRaises(mc_reuse.InventoryError) as cm:
            mc_reuse.load_inventory(d / "x.json")
        self.assertIn("not valid JSON", str(cm.exception))
        (d / "y.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(mc_reuse.InventoryError):
            mc_reuse.load_inventory(d / "y.json")

    def test_load_for_code_root_tells_absent_from_present_but_unusable(self):
        root = _mini(self, {"a.py": "def f():\n    pass\n"})
        self.assertEqual(mc_reuse.load_for_code_root(root), (None, None))
        (root / "docs").mkdir()
        (root / "docs" / "REUSE-INVENTORY.json").write_text("{nope", encoding="utf-8")
        inv, reason = mc_reuse.load_for_code_root(root)
        self.assertIsNone(inv)
        self.assertIn("not valid JSON", reason)
        self.assertNotIn("\n", reason)
        (root / "docs" / "REUSE-INVENTORY.json").write_text(json.dumps(_inv(_row(policy="whatever"))), encoding="utf-8")
        inv, reason = mc_reuse.load_for_code_root(root)
        self.assertIsNone(inv)
        self.assertIn("policy must be one of", reason)
        (root / "docs" / "REUSE-INVENTORY.json").write_text(json.dumps(_inv(_row())), encoding="utf-8")
        inv, reason = mc_reuse.load_for_code_root(root)
        self.assertEqual((inv["rows"][0]["id"], reason), ("r1", None))

    def test_missing_symbol_is_reported_with_the_path(self):
        root = _mini(self, {"a.py": "def g():\n    pass\n"})
        found = mc_reuse.check_inventory(root, _inv(_row()))
        self.assertEqual(len(found), 1, found)
        self.assertIn("r1: a.py#f: symbol not found in a.py", found[0])

    def test_empty_scan_is_an_error(self):
        root = _mini(self, {"notes.txt": "nothing\n"})
        found = mc_reuse.check_inventory(root, _inv(_row()))
        self.assertTrue(any("empty scan" in f for f in found), found)

    def test_unsupported_registered_shell_syntax_is_an_actionable_diagnostic(self):
        root = _mini(self, {"a.sh": "tricky() (\n    echo hi\n)\n"})
        found = mc_reuse.check_inventory(root, _inv(_row(implementation="a.sh#tricky")))
        self.assertTrue(any("unsupported shell function form" in f for f in found), found)
        self.assertTrue(any("could not read this declaration" in f for f in found), found)

    def test_unresolvable_test_identifier_is_reported(self):
        root = _mini(self, {"a.py": "def f():\n    pass\n", "tests/test_x.py": "class TestA:\n    def test_one(self):\n        pass\n"})
        ok = _row(test="tests.test_x.TestA.test_one")
        inv = _inv(ok)
        del inv["tests_dir"]
        self.assertEqual(mc_reuse.check_inventory(root, inv), [])
        for bad in ("tests.test_x.Nope", "tests.test_missing.TestA", "tests.test_x.TestA.nope", "test_x",
                    "tests.test_x.TestA.test_one.not_a_test"):
            inv = _inv(_row(test=bad))
            del inv["tests_dir"]
            found = mc_reuse.check_inventory(root, inv)
            self.assertTrue(any("test " in f for f in found), (bad, found))

    def test_unknown_related_path_is_reported(self):
        root = _mini(self, {"a.py": "def f():\n    pass\n"})
        found = mc_reuse.check_inventory(root, _inv(_row(related_paths=["nowhere/*.py"])))
        self.assertTrue(any("matches nothing" in f for f in found), found)


class TestValidationHoles(unittest.TestCase):
    """A row that guards nothing, or names something that does not resolve, is an error."""

    def check(self, files, row, **over):
        root = _mini(self, files)
        return mc_reuse.check_inventory(root, _inv(row, **over), check_tests=False)

    def test_name_pattern_that_does_not_match_its_owner_fails_with_both_names(self):
        row = _row(guards=[{"method": "name_owner", "params": {"pattern": "^symbol_that_does_not_exist$"}}])
        f = self.check({"a.py": "def f():\n    pass\n"}, row)
        self.assertEqual(len(f), 1, f)
        self.assertIn("'^symbol_that_does_not_exist$'", f[0])
        self.assertIn("'f'", f[0])
        self.assertIn("does not match the owner's name", f[0])

    def test_matching_name_pattern_and_default_pattern_are_fine(self):
        for params in ({"pattern": "^_?f$"}, {}):
            row = _row(guards=[{"method": "name_owner", "params": params}])
            self.assertEqual(self.check({"a.py": "def f():\n    pass\n"}, row), [], params)

    def test_unresolved_allow_entry_is_an_error_not_a_silent_no_op(self):
        row = _row(guards=[{"method": "name_owner", "params": {"allow": ["missing.py#missing"]}}])
        f = self.check({"a.py": "def f():\n    pass\n"}, row)
        self.assertEqual(len(f), 1, f)
        self.assertIn("allow entry", f[0])
        self.assertIn("missing.py#missing", f[0])
        good = _row(guards=[{"method": "name_owner", "params": {"pattern": "^[fg]$", "allow": ["a.py#g"]}}])
        self.assertEqual(self.check({"a.py": "def f():\n    pass\n\ndef g():\n    pass\n"}, good), [])

    def test_delegation_ref_must_be_the_owner_or_a_declared_alias(self):
        files = {"a.py": "def f(x):\n    return x\n", "c.py": "def g(t):\n    return t.read_text()\n"}
        row = _deleg_row(["c.py"], refs=["read_text"])
        f = self.check(files, row)
        self.assertTrue(any("ref 'read_text' is neither the owner's name 'f' nor a declared alias" in x
                            for x in f), f)

    def test_delegation_alias_must_be_a_variant_of_the_row(self):
        files = {"a.py": "def f(x):\n    return x\n", "c.py": "def g(t):\n    return t.read_text()\n"}
        f = self.check(files, _deleg_row(["c.py"], refs=["read_text"], aliases=["read_text"]))
        self.assertTrue(any("alias 'read_text' is not the name of one of the row's variants" in x for x in f), f)

    def test_declared_alias_is_accepted(self):
        files = {"a.py": "def f(x):\n    return x\n\ndef f_wrap(x):\n    return f(x)\n",
                 "c.py": "from a import f_wrap\n\ndef g(t):\n    return f_wrap(t)\n"}
        row = _deleg_row(["c.py"], refs=["f", "f_wrap"], aliases=["f_wrap"])
        row["variants"] = [{"symbol": "a.py#f_wrap", "contract": "c", "reason": "adapter"}]
        self.assertEqual(self.check(files, row), [])

    def test_test_id_with_trailing_components_is_rejected(self):
        root = _mini(self, {"a.py": "def f():\n    pass\n",
                            "tests/test_x.py": "class TestA:\n    def test_one(self):\n        pass\n"})
        inv = _inv(_row(test="tests.test_x.TestA.test_one.not_a_test"))
        del inv["tests_dir"]
        f = mc_reuse.check_inventory(root, inv)
        self.assertTrue(any("nothing after the method" in x for x in f), f)

    def test_missing_tests_directory_fails_unless_the_inventory_says_it_has_none(self):
        root = _mini(self, {"a.py": "def f():\n    pass\n"})
        inv = _inv(_row())
        del inv["tests_dir"]
        f = mc_reuse.check_inventory(root, inv)
        self.assertTrue(any("tests directory missing" in x for x in f), f)
        self.assertEqual(mc_reuse.check_inventory(root, _inv(_row())), [])            # tests_dir: null
        self.assertEqual(mc_reuse.check_inventory(root, inv, check_tests=False), [])  # explicit per-call flag

    def test_tests_dir_other_than_null_is_a_schema_error(self):
        errs = mc_reuse.validate_schema(_inv(_row(), tests_dir="tests"))
        self.assertTrue(any("tests_dir" in e for e in errs), errs)


class TestScanner(unittest.TestCase):
    def scan(self, files):
        root = _mini(self, files)
        return mc_reuse.scan_tree(root, ["."], ["tests"])

    def test_shell_declaration_forms(self):
        s = self.scan({"a.sh": '''\
            #!/usr/bin/env bash
            one() { echo 1; }
            two() {
                echo 2
            }
            function three {
                echo 3
            }
            function four() {
                echo 4
            }
            five()
            {
                echo 5
            }
            # six() { not a function }
            '''})
        names = sorted(d.name for d in s.defs if d.lang == "sh")
        self.assertEqual(names, ["five", "four", "one", "three", "two"])
        self.assertEqual(s.problems, [])

    def test_python_definitions_include_methods_constants_nested_and_conditional(self):
        s = self.scan({"a.py": '''\
            X = ("a", "b")
            Y: int = 3
            class C:
                def m(self):
                    def inner():
                        pass
            if True:
                def cond():
                    pass
            '''})
        quals = sorted(d.qual for d in s.defs)
        self.assertEqual(quals, ["C.m", "C.m.inner", "X", "Y", "cond"])

    def test_python_definitions_are_found_under_every_statement_container(self):
        s = self.scan({"a.py": '''\
            import sys
            class C:
                if sys.argv:
                    def in_class_if(self):
                        pass
            match sys.argv:
                case [x]:
                    def in_match():
                        pass
            async def run(cm, items):
                async with cm:
                    def in_async_with():
                        pass
                async for i in items:
                    def in_async_for():
                        pass
            with open("f") as fh:
                def in_with():
                    pass
            for i in range(2):
                def in_for():
                    pass
            while False:
                def in_while():
                    pass
            try:
                def in_try():
                    pass
            except OSError:
                def in_except():
                    pass
            else:
                def in_else():
                    pass
            finally:
                def in_finally():
                    pass
            '''})
        quals = sorted(d.qual for d in s.defs)
        self.assertEqual(quals, sorted([
            "C.in_class_if", "in_match", "run", "run.in_async_with", "run.in_async_for", "in_with", "in_for",
            "in_while", "in_try", "in_except", "in_else", "in_finally"]))

    @unittest.skipUnless(hasattr(__import__("ast"), "TryStar"), "except* needs Python 3.11")
    def test_python_definitions_are_found_under_try_star(self):
        s = self.scan({"a.py": "try:\n    pass\nexcept* OSError:\n    def in_star():\n        pass\n"})
        self.assertEqual([d.qual for d in s.defs], ["in_star"])

    def test_embedded_payload_forms(self):
        s = self.scan({"h.sh": '''\
            #!/usr/bin/env bash
            "$MC_PY" - <<'EOF'
            def from_heredoc():
                pass
            EOF
            x="$(env PYTHONPATH= "$MC_PY" -c '
            def from_dash_c():
                return "it'\\''s"
            ')"
            cat <<'USAGE'
            def not_python_even_if_it_looks():
                pass
            USAGE
            echo "$MC_PY" -c "def double_quoted(): pass"
            '''})
        emb = sorted(d.name for d in s.defs if d.embedded)
        self.assertEqual(emb, ["from_dash_c", "from_heredoc"])
        self.assertEqual(s.problems, [])

    def test_dash_heredoc_strips_leading_tabs_from_the_payload(self):
        s = self.scan({"h.sh": '#!/usr/bin/env bash\n"$MC_PY" - <<-\'EOF\'\n\tdef tabbed():\n\t    pass\n\tEOF\n'})
        self.assertEqual([d.name for d in s.defs if d.embedded], ["tabbed"])
        self.assertEqual(s.problems, [])

    def test_unterminated_heredoc_does_not_stop_later_payloads_from_being_scanned(self):
        s = self.scan({"h.sh": "cat <<'NEVER'\ndata\n\"$MC_PY\" - <<'EOF'\ndef later():\n    pass\nEOF\n"})
        self.assertEqual(len(s.problems), 1)
        self.assertIn("h.sh:1:", s.problems[0])
        self.assertEqual([d.name for d in s.defs if d.embedded], ["later"])

    def test_quoted_heredoc_opener_and_quoted_interpreter_text_are_not_scan_problems(self):
        s = self.scan({"q.sh": '#!/usr/bin/env bash\necho "<<EOF"\necho "python3 -c \'not python !\'"\n'
                               'echo \'$PY - <<EOT\'\n'})
        self.assertEqual(s.problems, [])
        self.assertEqual([d for d in s.defs if d.embedded], [])

    def test_multi_line_quoted_text_holding_an_opener_or_an_interpreter_is_not_a_scan_problem(self):
        # the round-3 gate's two controls: a quote opened on one line is still open on the next
        for body in ('echo "\n<<EOF\n"\n', 'echo "\npython3 -c \'not python !\'\n"\n',
                     "echo '\n$PY - <<EOT\n'\n"):
            s = self.scan({"q.sh": "#!/usr/bin/env bash\n" + body})
            self.assertEqual(s.problems, [], body)
            self.assertEqual([d for d in s.defs if d.embedded], [], body)
        ft = FixtureTree(self)
        ft.append("hooks/post-commit-reindex.sh", '\necho "\n<<EOF\n"\necho "\npython3 -c \'not python !\'\n"\n')
        self.assertEqual(ft.findings(), [])

    def test_real_problems_after_a_multi_line_string_keep_their_line_numbers(self):
        s = self.scan({"h.sh": '#!/usr/bin/env bash\necho "a\nb"\ncat <<\'NEVER\'\ndata\n'})
        self.assertEqual(len(s.problems), 1)
        self.assertIn("h.sh:4:", s.problems[0])
        s = self.scan({"p.sh": "#!/usr/bin/env bash\necho \"a\nb\"\npython3 -c 'def (:'\n"})
        self.assertEqual(len(s.problems), 1)
        self.assertIn("p.sh:4:", s.problems[0])

    def test_a_heredoc_opener_on_a_continuation_line_still_masks_its_body(self):
        masked, payloads, problems = mc_reuse._mask_shell("cat \\\n  <<'EOT'\nnot code ( \" '\nEOT\necho after\n")
        self.assertEqual(masked, ["cat \\", "  <<'EOT'", "", "", "echo after", ""])
        self.assertEqual((payloads, problems), ([], []))

    def test_interpreter_invocations_count_after_assignments_exec_and_env_only(self):
        for line in ("X=1 python3 -c 'def one():\n    pass'", "exec python3 -c 'def one():\n    pass'",
                     "env X=1 python3 -c 'def one():\n    pass'", 'out=$("$PY" -c \'def one():\n    pass\')'):
            s = self.scan({"i.sh": "#!/usr/bin/env bash\n" + line + "\n"})
            self.assertEqual([d.name for d in s.defs if d.embedded], ["one"], line)
        s = self.scan({"i.sh": "#!/usr/bin/env bash\nxargs python3 -c 'def one():\n    pass'\n"})
        self.assertEqual([d for d in s.defs if d.embedded], [])

    def test_shell_text_drops_comments_and_heredoc_bodies(self):
        s = self.scan({"a.sh": '''\
            f() {
                echo hi  # trailing --against-ref comment
                echo "# not a comment --kept"
                cat <<'EOT'
            --against-ref in heredoc data
            EOT
            }
            '''})
        unit = next(u for u in s.units if u.qual == "f")
        self.assertNotIn("trailing", unit.text)
        self.assertNotIn("heredoc data", unit.text)
        self.assertIn("--kept", unit.text)

    def test_unparseable_production_python_is_a_scan_problem(self):
        s = self.scan({"a.py": "def (:\n", "b.py": "def ok():\n    pass\n"})
        self.assertEqual(len(s.problems), 1)
        self.assertIn("a.py", s.problems[0])

    def test_py_fingerprint_renames_locals_but_keeps_literals_and_globals(self):
        import ast
        a = ast.parse("def f(x, y=1):\n    '''d'''\n    t = x + y\n    return len(t)\n").body[0]
        b = ast.parse("def g(p, q=1):\n    r = p + q\n    return len(r)\n").body[0]
        c = ast.parse("def h(p, q=2):\n    r = p + q\n    return len(r)\n").body[0]
        d = ast.parse("def h(p, q=1):\n    r = p - q\n    return len(r)\n").body[0]
        e = ast.parse("def h(p, q=1):\n    r = p + q\n    return str(r)\n").body[0]
        self.assertEqual(mc_reuse.py_fingerprint(a), mc_reuse.py_fingerprint(b))
        nested = ast.parse('def g(p, q=1):\n    def k():\n        """inner prose"""\n        return 1\n'
                           '    r = p + q\n    return len(r)\n').body[0]
        plain = ast.parse("def g(p, q=1):\n    def k():\n        return 1\n    r = p + q\n    return len(r)\n").body[0]
        self.assertEqual(mc_reuse.py_fingerprint(nested), mc_reuse.py_fingerprint(plain))
        for other in (c, d, e):
            self.assertNotEqual(mc_reuse.py_fingerprint(a), mc_reuse.py_fingerprint(other))


class TestRender(unittest.TestCase):
    def inv(self):
        return mc_reuse.load_inventory(TOOLS_DIR / mc_reuse.INVENTORY_RELPATH)

    def test_rendering_is_deterministic_and_names_job_owner_guard_and_limit(self):
        inv = self.inv()
        a = mc_reuse.render_rows(inv["rows"])
        self.assertEqual(a, mc_reuse.render_rows(copy.deepcopy(inv["rows"])))
        row = next(r for r in inv["rows"] if r["id"] == "join-semicolon-list")
        text = mc_reuse.render_row(row)
        for want in ("join-semicolon-list", "scripts/mc-list-lib.sh#mc_join_semi", "name_owner", "exact_copy",
                     "guard misses:", "variant: scripts/memcontinuum-decide.sh#encode_join_semi"):
            self.assertIn(want, text)

    def test_known_debt_is_never_rendered_as_guarded(self):
        row = _row(policy="known_debt", guards=[],
                   variants=[{"symbol": "a.py#g", "contract": "c", "reason": "r"}], why_single="merge g into f")
        text = mc_reuse.render_row(row)
        self.assertIn("KNOWN DEBT, not guarded", text)
        self.assertIn("guards: none (known debt)", text)
        self.assertIn("intended resolution: merge g into f", text)
        self.assertIn("current implementation: a.py#g", text)
        self.assertNotIn("guarded by", text)

    def test_selection_by_owner_variant_and_related_paths(self):
        inv = self.inv()
        ids = lambda files: [r["id"] for r in mc_reuse.select_rows(inv, files)]   # noqa: E731
        self.assertIn("coverage-fact-sentence", ids(["hooks/mc_nudge.py"]))
        self.assertIn("coverage-fact-sentence", ids(["./hooks/userprompt-remind.sh"]))
        self.assertIn("link-classification", ids(["memlint.py"]))
        self.assertIn("chunker-chunk-file", ids(["chunkers/swift.py"]))
        self.assertEqual(ids(["README.md"]), [])
        self.assertEqual(ids([]), [])


if __name__ == "__main__":
    unittest.main()
