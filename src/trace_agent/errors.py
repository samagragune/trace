"""Root of Trace's exception hierarchy.

Every error Trace raises *on purpose* derives from :class:`TraceError`, so the
CLI can tell "a condition we anticipated and can explain" apart from "a bug".
Module-specific subclasses live next to the code that raises them.
"""


class TraceError(Exception):
    """Base class for all deliberate Trace errors."""
