"""Vector clocks used to tell a real conflict apart from an ordinary
sequential edit. See TOP-108."""


def compare_clocks(a, b):
    """Return "before", "after", "equal", or "concurrent" for two vector
    clocks `a` and `b`."""
    if a == b:
        return "equal"
    a_leq_b = all(a.get(k, 0) <= v for k, v in b.items())
    b_leq_a = all(b.get(k, 0) <= v for k, v in a.items())
    if a_leq_b and not b_leq_a:
        return "before"
    if b_leq_a and not a_leq_b:
        return "after"
    return "concurrent"
