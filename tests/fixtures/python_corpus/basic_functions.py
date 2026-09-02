"""Fixture: module-level and nested function shapes for the Python ast chunker."""


def plain_function(a, b=1):
    """First line of the docstring.

    More detail that must not appear in `doc`.
    """
    return a + b


async def fetch_data(url: str) -> str:
    """Fetch a URL asynchronously."""
    return url


@memoize
def decorated_standalone(x):
    """A module function preceded by a decorator."""
    return x


def outer_with_nested(n):
    """Outer function containing a nested function."""

    def inner(step):
        return step + 1

    return inner(n)
