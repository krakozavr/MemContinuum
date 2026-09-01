import importlib.util
import unittest
import chunkers


def _backends_available():
    return bool(
        importlib.util.find_spec("chunkers.swift")
        and importlib.util.find_spec("chunkers.python_ast")
    )


class TestRegistry(unittest.TestCase):
    def test_kinds_frozen(self):
        self.assertEqual(chunkers.KINDS,
            frozenset({"function", "method", "constructor", "accessor", "closure"}))

    def test_lang_for_path(self):
        self.assertEqual(chunkers.lang_for_path("a/b.swift"), "swift")
        self.assertEqual(chunkers.lang_for_path("x.py"), "python")
        self.assertIsNone(chunkers.lang_for_path("x.rs"))       # not in M1 table
        self.assertIsNone(chunkers.lang_for_path("x.blade.php"))  # compound ext never a plain match

    @unittest.skipUnless(_backends_available(),
        "chunkers.swift / chunkers.python_ast land in Tasks 2/4")
    def test_get_chunker_has_chunk_file(self):
        for lang in ("swift", "python"):
            self.assertTrue(callable(chunkers.get_chunker(lang).chunk_file))

    def test_chunker_version_changes_with_impl_version(self):
        v1 = chunkers.chunker_version("swift")
        self.assertRegex(v1, r"^[0-9a-f]{12}$")
        old = chunkers.LANGUAGE_TABLE["swift"]["impl_version"]
        try:
            chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = "TEST-BUMP"
            self.assertNotEqual(chunkers.chunker_version("swift"), v1)
        finally:
            chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = old
