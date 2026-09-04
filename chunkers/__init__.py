"""Chunker registry: contract, kind vocabulary, and per-language backend table.

Backends (`chunkers.swift`, `chunkers.python_ast`, ...) are imported lazily
via `get_chunker` so this package imports cleanly even before every backend
module exists (Tasks 2/4 add the Swift and Python backends). Exclusivity
(spec S1a) is structural: `LANGUAGE_TABLE` has exactly one row per language;
there is no API to register a second backend for an existing language.
"""

import hashlib
import importlib
import os

KINDS = frozenset({"function", "method", "constructor", "accessor", "closure"})

# Compound extensions that must never match a plain single-suffix lookup,
# even if a future language row's extension happens to be a suffix of one
# of these (e.g. ".php" would otherwise false-match "x.blade.php").
COMPOUND_EXCLUDES = {".blade.php", ".d.ts", ".min.js"}

# Task 6: the ONE universal noise set -- directory names that are never
# user source regardless of which language(s) are wired, or of whether any
# language is wired at all (memidx.CODE_SKIP_DIR_NAMES is an alias of this,
# and iter_code_files -- the walker `why`'s fallback and `drift`'s
# invariants share -- prunes exactly this set, nothing narrower). ".build"
# lives here, not on the swift row alone: it is SwiftPM's dependency
# checkout, thousands of third-party .swift files that are never user
# source in ANY project, Swift-wired or not -- a bare `why`/`drift` walk
# with no language wired at all must still skip it. Each LANGUAGE_TABLE
# row's own "skip_dirs" holds only the noise names that are NOT already
# covered here (fix wave C1's per-language intersection rule, unaffected).
UNIVERSAL_SKIP_DIRS = frozenset({
    ".git", ".build", "node_modules", "vendor", "venv", ".venv",
    "__pycache__", ".tox", ".eggs",
})

LANGUAGE_TABLE = {
    "swift": {"backend": "native", "module": "chunkers.swift",
              "extensions": (".swift",), "shebangs": (), "impl_version": "3",
              "skip_dirs": frozenset({"Tests", "Resources"})},
    "python": {"backend": "native", "module": "chunkers.python_ast",
               "extensions": (".py",), "shebangs": ("python", "python3"),
               "impl_version": "1",
               "skip_dirs": frozenset({"build", "dist"})},
    "javascript": {"backend": "tree-sitter", "module": "chunkers.treesitter",
                    "grammar_module": "tree_sitter_javascript", "language_fn": "language",
                    "runtime_pin": "0.26.0", "grammar_pin": "0.25.0",
                    "query_file": "javascript.scm", "impl_version": "1",
                    "extensions": (".js", ".jsx", ".mjs", ".cjs"), "shebangs": ("node",),
                    "skip_dirs": frozenset({"build", "dist"}),
                    "containers": {"class_declaration": "name"},
                    "method_if_ancestor_in": frozenset(), "max_bytes": None},
    "typescript": {"backend": "tree-sitter", "module": "chunkers.treesitter",
                    "grammar_module": "tree_sitter_typescript", "language_fn": "language_typescript",
                    "runtime_pin": "0.26.0", "grammar_pin": "0.23.2",
                    "query_file": "typescript.scm", "impl_version": "1",
                    "extensions": (".ts",), "shebangs": (),
                    "skip_dirs": frozenset({"build", "dist"}),
                    "containers": {"class_declaration": "name", "internal_module": "name"},
                    "method_if_ancestor_in": frozenset(), "max_bytes": None},
    "tsx": {"backend": "tree-sitter", "module": "chunkers.treesitter",
            "grammar_module": "tree_sitter_typescript", "language_fn": "language_tsx",
            "runtime_pin": "0.26.0", "grammar_pin": "0.23.2",
            "query_file": "typescript.scm", "impl_version": "1",
            "extensions": (".tsx",), "shebangs": (),
            "skip_dirs": frozenset({"build", "dist"}),
            "containers": {"class_declaration": "name", "internal_module": "name"},
            "method_if_ancestor_in": frozenset(), "max_bytes": None},
    "java": {"backend": "tree-sitter", "module": "chunkers.treesitter",
             "grammar_module": "tree_sitter_java", "language_fn": "language",
             "runtime_pin": "0.26.0", "grammar_pin": "0.23.5",
             # impl_version stands at "2" from the fix round that taught this
             # row to read real Javadoc/line-comment text into a chunk's `doc`
             # field. That bump is no longer the mechanism it was: whole-branch
             # review finding 4 folded doc_comment_types (and containers,
             # method_if_ancestor_in, max_bytes) into chunker_version's payload
             # through treesitter.row_shape, so editing any of them invalidates
             # this row on its own. The value stays as it is because lowering it
             # would only re-collide with a fingerprint some index may already
             # hold; impl_version remains the per-row escape hatch for a change
             # no other field in the payload can express.
             "query_file": "java.scm", "impl_version": "2",
             "extensions": (".java",), "shebangs": (),
             "skip_dirs": frozenset({"target", "build", "dist"}),
             "containers": {"class_declaration": "name", "interface_declaration": "name",
                             "enum_declaration": "name", "record_declaration": "name"},
             "method_if_ancestor_in": frozenset(), "max_bytes": None,
             "doc_comment_types": ("block_comment", "line_comment")},
    "php": {"backend": "tree-sitter", "module": "chunkers.treesitter",
            "grammar_module": "tree_sitter_php", "language_fn": "language_php",
            "runtime_pin": "0.26.0", "grammar_pin": "0.24.1",
            "query_file": "php.scm", "impl_version": "1",
            "extensions": (".php",), "shebangs": (),
            "skip_dirs": frozenset(),
            "containers": {"class_declaration": "name", "trait_declaration": "name",
                            "enum_declaration": "name"},
            "method_if_ancestor_in": frozenset(), "max_bytes": None},
    "rust": {"backend": "tree-sitter", "module": "chunkers.treesitter",
             "grammar_module": "tree_sitter_rust", "language_fn": "language",
             "runtime_pin": "0.26.0", "grammar_pin": "0.24.2",
             "query_file": "rust.scm", "impl_version": "1",
             "extensions": (".rs",), "shebangs": (),
             "skip_dirs": frozenset({"target"}),
             "containers": {"impl_item": "SELF_TYPE", "trait_item": "name", "mod_item": "name"},
             "method_if_ancestor_in": frozenset({"impl_item", "trait_item"}), "max_bytes": None,
             # Task 7: rust's own comment node types are "line_comment" and
             # "block_comment" (verified against the real grammar), not the
             # `_doc_for` default of ("comment",) -- a `///` doc comment is
             # NOT a distinct top-level node type in this grammar version;
             # it parses as an ordinary `line_comment` node whose children
             # (`outer_doc_comment_marker`, `doc_comment`) carry the `///`
             # marking internally, so the row-level override below is what
             # makes ANY comment (doc or plain) reach a rust chunk's `doc`
             # field at all. This field is part of chunker_version's payload
             # (treesitter.row_shape), so editing it invalidates this row's
             # indexed files on its own.
             "doc_comment_types": ("line_comment", "block_comment")},
    "lua": {"backend": "tree-sitter", "module": "chunkers.treesitter",
            "grammar_module": "tree_sitter_lua", "language_fn": "language",
            "runtime_pin": "0.26.0", "grammar_pin": "0.5.0",
            "query_file": "lua.scm", "impl_version": "1",
            "extensions": (".lua",), "shebangs": ("lua",),
            "skip_dirs": frozenset(),
            "containers": {},
            "method_if_ancestor_in": frozenset(), "max_bytes": None},
}


class ChunkResult:
    """Plain result container, stdlib only."""

    def __init__(self, chunks, gaps, status):
        self.chunks = chunks        # list[dict]: kind, symbol, qualified_name,
                                     # signature, doc, start_line, end_line, lang
        self.gaps = gaps            # list[(start_line, end_line, reason:str)]
        self.status = status        # "ok" | "partial" | "failed"


class BackendUnavailable(Exception):
    """A LANGUAGE_TABLE backend that cannot run here (missing wheel,
    provider init failure). The indexer records the file as not-indexed
    and retries on the next explicit run, or when availability changes."""


def get_chunker(lang):
    """Return the backend module for `lang` (must expose `chunk_file`).

    Imports lazily via importlib so this registry module imports cleanly
    before every backend exists. importlib.import_module does nothing but
    import here -- ANY exception it raises (not just ImportError: a
    provider's module-level init can raise RuntimeError, OSError, its own
    exception type, ...) means this engine cannot run this backend on this
    machine, so it is wrapped as BackendUnavailable so cmd_code_reindex's
    per-file guard can tell "this file is broken" (a deterministic
    failure) apart from "this engine can't run this backend here"
    (not-indexed, retried when that changes) -- fail-open all the way up
    through backend_availability()/code_index_report() into code-search
    and why, none of which may crash on a backend's own import bug.
    """
    row = LANGUAGE_TABLE[lang]
    if row["backend"] == "native":
        try:
            return importlib.import_module(row["module"])
        except Exception as exc:
            raise BackendUnavailable(f"{lang}: {type(exc).__name__}: {exc}") from exc
    if row["backend"] == "tree-sitter":
        from . import treesitter
        return treesitter.for_language(lang)
    raise BackendUnavailable(f"{lang}: unknown backend {row['backend']!r}")


def backend_availability():
    """'lang=ok;lang=missing;...' over every table row, sorted -- the
    fingerprint a not-indexed file_sha row stamps as its attempt_key, and
    that code-reindex/heal compare against on a later run to decide
    whether a not-indexed row is worth another try (Anatomy M2a binding
    point 1). Recomputed on every call (no internal caching) -- callers
    that need it more than once per run cache the single value locally."""
    parts = []
    for lang in sorted(LANGUAGE_TABLE):
        try:
            get_chunker(lang)
            parts.append(f"{lang}=ok")
        except BackendUnavailable:
            parts.append(f"{lang}=missing")
    return ";".join(parts)


def extension_of(path):
    """The extension a census/provenance tally should key `path` under.

    Compound-extension aware (Anatomy M1 fix wave, I2): a file named
    `foo.blade.php` is keyed ".blade.php", NOT the ".php" a plain
    `os.path.splitext` would return -- lang_for_path already refuses to
    treat the two as the same thing (COMPOUND_EXCLUDES), so a tally that
    folded them together would describe a tree neither surface agrees
    exists. Returns "" for an extensionless file, exactly like splitext.

    Single source for both tallies that need it (memidx.code_census and
    code-reindex's skipped-extension provenance line) rather than a
    splitext call duplicated at each site.
    """
    basename = os.path.basename(os.fspath(path))
    for excluded in COMPOUND_EXCLUDES:
        if basename.endswith(excluded) and basename != excluded:
            return excluded
    _, ext = os.path.splitext(basename)
    return ext


def lang_for_path(path):
    """Extension (compound-ext aware) -> lang name, else None.

    A compound extension in COMPOUND_EXCLUDES is checked first (longest
    match wins in practice since these are literal known compounds) and
    always returns None, so it can never accidentally match a plain
    single-suffix language extension.
    """
    basename = os.path.basename(os.fspath(path))
    for excluded in COMPOUND_EXCLUDES:
        if basename.endswith(excluded):
            return None
    _, ext = os.path.splitext(basename)
    for lang, row in LANGUAGE_TABLE.items():
        if ext in row["extensions"]:
            return lang
    return None


def chunker_version(lang):
    """sha256 of backend+module+impl_version, first 12 hex chars.

    Reads LANGUAGE_TABLE fresh on every call (no caching) so a patched
    impl_version is reflected immediately.

    A tree-sitter row's payload is wider, because a tree-sitter row's
    output depends on more than its own module name: the shared engine's
    ENGINE_VERSION (one generic module produces every tree-sitter row's
    chunks, so its behavior changes all seven at once), the pinned runtime
    and grammar versions, the query file's bytes, and the row's own
    chunk-shaping data through treesitter.row_shape -- containers,
    method_if_ancestor_in, doc_comment_types, max_bytes. Everything that
    changes what a chunk looks like is in here; `impl_version` remains the
    per-row escape hatch on top for anything that is not.
    """
    row = LANGUAGE_TABLE[lang]
    if row["backend"] == "native":
        payload = f"{row['backend']}:{row['module']}:{row['impl_version']}"
    elif row["backend"] == "tree-sitter":
        from . import treesitter
        payload = (f"{row['backend']}:{row['module']}:{treesitter.ENGINE_VERSION}:"
                   f"{row['runtime_pin']}:"
                   f"{row['grammar_module']}:{row['grammar_pin']}:"
                   f"{treesitter.query_fingerprint(row)}:{treesitter.row_shape(row)}:"
                   f"{row['impl_version']}")
    else:
        payload = f"{row['backend']}:{row.get('module','?')}:{row.get('impl_version','?')}"
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def known_extensions():
    """Every extension in the table."""
    exts = set()
    for row in LANGUAGE_TABLE.values():
        exts.update(row["extensions"])
    return exts


def wired_extensions(langs):
    """Extensions for a chosen language subset."""
    exts = set()
    for lang in langs:
        exts.update(LANGUAGE_TABLE[lang]["extensions"])
    return exts


def lang_for_shebang(first_line):
    """Shebang first line (e.g. "#!/usr/bin/env python3") -> lang name via
    LANGUAGE_TABLE's per-row "shebangs" stems, else None.

    Task 8 (Anatomy M1): memidx.py's code-census uses this to classify an
    EXTENSIONLESS executable script -- lang_for_path (extension-only) can
    never resolve one, so an unwired-but-recognizable interpreter would
    otherwise silently vanish from the census (the "second silent gap" a
    Task 5 reviewer flagged: a repo can have real Python entry-point
    scripts with no `.py` suffix at all).

    Matching: split the line after "#!" on whitespace; if the first token's
    basename is "env" (the common `#!/usr/bin/env python3` indirection),
    the interpreter name is the NEXT token's basename instead, else it's
    the first token's own basename. A row's "shebangs" stem matches when
    the interpreter name equals it OR STARTS WITH it -- so "python3",
    "python3.11", "python3.12" etc. all match the "python"/"python3" stems
    without the table enumerating every patch version (the brief's
    "first line #!...python* -> counted as python" wildcard). Iterates
    LANGUAGE_TABLE once; no stem across the current rows overlaps another
    row's, so match order never matters.
    """
    if not first_line.startswith("#!"):
        return None
    parts = first_line[2:].split()
    if not parts:
        return None
    interpreter = os.path.basename(parts[0])
    if interpreter == "env" and len(parts) > 1:
        interpreter = os.path.basename(parts[1])
    if not interpreter:
        return None
    for lang, row in LANGUAGE_TABLE.items():
        for stem in row.get("shebangs", ()):
            if interpreter == stem or interpreter.startswith(stem):
                return lang
    return None


def skip_dirs_for_lang(lang):
    """The noise directory names `lang` itself wants pruned -- empty for a
    language with no LANGUAGE_TABLE row (fail open, never raise)."""
    return frozenset(LANGUAGE_TABLE.get(lang, {}).get("skip_dirs", ()))


def common_skip_dirs(langs):
    """Directory names EVERY wired language would drop anyway -- the
    INTERSECTION of the wired languages' skip sets (Anatomy M1 fix wave,
    C1; supersedes the old union rule).

    Why an intersection and not a union: a skip set belongs to ONE
    language and describes ITS noise. Swift's names "Tests"; python's does
    not. Pruning the union made a directory named by ANY wired language
    invisible to EVERY wired language, so wiring swift alongside python
    silently dropped every `Tests/*.py` file in the project -- real source
    the census had just proposed python on the strength of. The Codex gate
    ruled that data loss, not an accepted trade-off.

    So the walk prunes only what is safe to prune for everyone: the global
    noise dirs plus this intersection (a pure optimization -- every file
    under such a directory would be dropped by its own language's rule
    anyway). Every other directory is walked, and the per-file decision
    (see `dir_is_skipped_for_lang`) drops a file iff one of its ancestor
    directory names sits in ITS OWN language's skip set.

    Languages with no LANGUAGE_TABLE row contribute nothing and are
    ignored here rather than emptying the intersection -- an unknown name
    would otherwise silently switch the optimization off. (code-reindex
    itself now rejects an unknown --lang outright; this only keeps the
    helper honest for any other caller.)"""
    sets = [skip_dirs_for_lang(l) for l in langs if l in LANGUAGE_TABLE]
    if not sets:
        return set()
    common = set(sets[0])
    for s in sets[1:]:
        common &= s
    return common


def path_is_skipped_for_lang(rel_parts, lang):
    """True when any ancestor DIRECTORY name in `rel_parts` (a file path's
    parts RELATIVE to the code root, its own basename included or not --
    only the directory components are consulted) is in `lang`'s own
    skip set. The parts must be root-relative: a code root that is itself
    named `Tests` must not have its whole contents dropped."""
    skip = skip_dirs_for_lang(lang)
    if not skip:
        return False
    return any(part in skip for part in rel_parts[:-1])
