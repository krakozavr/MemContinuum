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
              "extensions": (".swift",), "shebangs": (), "impl_version": "1"},
    "python": {"backend": "native", "module": "chunkers.python_ast",
               "extensions": (".py",), "shebangs": ("python", "python3"),
               "impl_version": "1"},
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
