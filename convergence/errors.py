"""Shared exception types (kept dependency-free to avoid import cycles)."""

from __future__ import annotations


class ConvergenceError(Exception):
    """A user-facing failure (fail loud, never guess)."""


class LockBusy(ConvergenceError):
    """Another convergence operation already holds this project's lock."""
