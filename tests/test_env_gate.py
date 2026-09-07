"""process-note-review-dispatch-python-env: the suite must fail LOUDLY, not
skip silently, when $MEMCONTINUUM_PYTHON is unset.

Today, dozens of whole test classes (tree-sitter chunkers, embeddings,
repo-init dependency reconciliation, the real-bash write-hook suites, ...)
are gated with `@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)` -- correct
and deliberate (a machine without the pinned venv genuinely cannot run
them), but the net effect on a machine that forgot to set the variable is a
quiet "OK (skipped=N)" that reads exactly like a healthy, fully-covered run.
This one guard test fails the run instead, naming the missing variable and
how much coverage silently vanished, unless the operator explicitly opts
into an ungated run with $MEMCONTINUUM_ALLOW_UNGATED=1 -- CI already sets
$MEMCONTINUUM_PYTHON (.github/workflows/tests.yml), so this never fires
there.
"""
import os
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _venv_gated_test_classes():
    """Every TestCase class, across tests/test_*.py, that unittest itself
    has marked skipped (`__unittest_skip__` set on the CLASS, not just an
    individual method) for a venv-related reason -- discovered by
    re-walking the same suite the real run already built (test modules are
    already in sys.modules by the time this runs, so `discover` re-imports
    nothing; it only re-collects), so this reflects the CURRENT process's
    real environment exactly, including subclasses that inherit their
    gating from a decorated base class (e.g. tests/test_update.py's
    UpdateTestBase) rather than carrying their own decorator."""
    loader = unittest.defaultTestLoader
    suite = loader.discover(start_dir=str(TESTS_DIR), pattern="test_*.py")
    seen: set = set()
    gated: list = []

    def walk(node):
        for item in node:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            else:
                cls = item.__class__
                if cls in seen:
                    continue
                seen.add(cls)
                if getattr(cls, "__unittest_skip__", False):
                    why = getattr(cls, "__unittest_skip_why__", "") or ""
                    if "MEMCONTINUUM_PYTHON" in why:
                        gated.append(cls)

    walk(suite)
    return gated


class TestSuiteRefusesToSkipTheVenvGateSilently(unittest.TestCase):
    def test_memcontinuum_python_must_be_set_or_explicitly_waived(self):
        if os.environ.get("MEMCONTINUUM_PYTHON", ""):
            return  # the common case -- nothing to guard
        if os.environ.get("MEMCONTINUUM_ALLOW_UNGATED", "") == "1":
            return  # explicit operator opt-in -- accepted, not silent
        gated = _venv_gated_test_classes()
        self.fail(
            f"$MEMCONTINUUM_PYTHON is not set: {len(gated)} test class(es) will "
            "silently skip every one of their tests (tree-sitter chunkers, "
            "embeddings, repo-init dependency reconciliation, the real-bash "
            "write-hook suites, ...) -- see README.md's 'Running the tests' "
            "section. Set $MEMCONTINUUM_PYTHON to a venv python with the pinned "
            "dependencies installed, or set $MEMCONTINUUM_ALLOW_UNGATED=1 to run "
            "without it anyway, accepting the skipped coverage."
        )
