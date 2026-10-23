"""Standard-library-only fixture checks for the separate native Hermes runtime."""


def require_first(values):
    try:
        return next(values)
    except StopIteration:
        raise AssertionError("Required native test fixture is missing") from None
