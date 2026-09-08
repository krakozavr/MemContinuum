"""Fixture test file for the bench corpus -- exercises the dedup store.

Not a real test suite: this file exists so CON-301 ("Dedup Store") has a
tested_by target. It is never collected by the engine's own unittest
discovery (outside tests/), see bench/README.md.
"""


def test_put_then_get_roundtrips():
    pass


def test_gc_sweep_removes_only_zero_refcount():
    pass
