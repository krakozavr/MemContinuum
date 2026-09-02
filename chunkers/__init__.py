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

LANGUAGE_TABLE = {
    "swift": {"backend": "native", "module": "chunkers.swift",
              "extensions": (".swift",), "shebangs": (), "impl_version": "3",
              "skip_dirs": frozenset({"Tests", "Resources", ".build"})},
    "python": {"backend": "native", "module": "chunkers.python_ast",
               "extensions": (".py",), "shebangs": ("python", "python3"),
               "impl_version": "1",
               "skip_dirs": frozenset({"venv", ".venv", "__pycache__", "build",
                                        "dist", ".tox", ".eggs"})},
}


class ChunkResult:
    """Plain result container, stdlib only."""

    def __init__(self, chunks, gaps, status):
        self.chunks = chunks        # list[dict]: kind, symbol, qualified_name,
                                     # signature, doc, start_line, end_line, lang
        self.gaps = gaps            # list[(start_line, end_line, reason:str)]
        self.status = status        # "ok" | "partial" | "failed"


def get_chunker(lang):
    """Return the backend module for `lang` (must expose `chunk_file`).

    Imports lazily via importlib so this registry module imports cleanly
    before every backend exists.
    """
    module_name = LANGUAGE_TABLE[lang]["module"]
    return importlib.import_module(module_name)


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
    """
    row = LANGUAGE_TABLE[lang]
    payload = f"{row['backend']}:{row['module']}:{row['impl_version']}"
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


def wired_skip_dirs(langs):
    """Directory names to prune from a walk, UNIONED across a chosen
    language subset (Task 7, Anatomy M1 milestone).

    Deliberately-resolved design note (brief Step 1(b)): pruning is a
    single decision made once per directory for the WHOLE walk, using the
    union of every wired lang's skip_dirs -- not a per-language decision
    re-made for each wired lang. So a directory named in ANY wired lang's
    skip_dirs is pruned for ALL of them, even a lang whose own skip_dirs
    entry would never have pruned it standalone. Concretely: swift's
    skip_dirs includes "Tests"; wiring swift alongside python prunes
    "Tests/" from the walk entirely, so a `Tests/*.py` file is invisible
    to python's chunker too -- accepted trade-off (simpler than a
    per-lang-aware walk), not a bug. Only after this union prune does
    per-file classification (chunkers.lang_for_path) filter by extension.

    Fails open on a `lang` with no LANGUAGE_TABLE row (a stray/typo'd
    --lang value, or a language the engine doesn't chunk yet): contributes
    no skip_dirs rather than raising, matching cmd_code_reindex's existing
    per-file tolerance for the same case (chunker_version falls back to
    "unversioned") and the documented behavior that naming an unwired
    language just matches zero files, silently -- never a crash."""
    dirs = set()
    for lang in langs:
        dirs.update(LANGUAGE_TABLE.get(lang, {}).get("skip_dirs", ()))
    return dirs
