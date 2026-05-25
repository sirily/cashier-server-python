"""Errors raised while building a safe standalone PWA ledger snapshot."""


class SnapshotBuildError(Exception):
    """The ledger cannot be exported without unproven semantic changes."""
