"""A nested def inside a METHOD -- the one qualification shape the other
python_corpus fixtures do not cover (T2, Anatomy M1 fix wave).

basic_functions.py has a nested def inside a plain FUNCTION, and
classes.py has a method inside a nested CLASS; neither pins what happens
when a def is nested inside a method, where the qualification stack mixes
a class name and a function name. That form decides both the emitted
`kind` (the immediate parent is a function, not a class, so the inner def
is a plain "function", never a "method") and the dotted qualified_name.
"""


class Outer:
    """A class whose method defines a helper inside itself."""

    def method(self):
        """Outer method."""

        def inner():
            """The nested helper."""
            return 1

        return inner()

    def plain(self):
        return 2
