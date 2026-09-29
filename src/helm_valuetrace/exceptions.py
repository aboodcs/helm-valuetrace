"""Exception classes for Helm ValueTrace."""

from __future__ import annotations


class ValueTraceError(Exception):
    """Raised for input errors that should be shown without a traceback (exit code 1)."""


class ChartError(ValueTraceError):
    """Raised for chart archive, Chart.yaml, or dependency errors (exit code 3)."""
