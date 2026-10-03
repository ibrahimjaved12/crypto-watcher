"""In-process typed validation receipts for an exclusively owned unchanged tree.

Never serialized or accepted from another process/job. Mutation invalidates the
receipt; a new trust boundary still performs full semantic validation.
"""
from dataclasses import dataclass
from pathlib import Path

from .historical_study_bundles import regular


def signature(path):
    info = regular(path)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class SourceValidation:
    period_index: int
    coverage_sha256: str
    evidence: dict | None
    files: tuple

    def current(self, period, coverage):
        if self.period_index != period.study_period_index or self.coverage_sha256 != coverage['coverage_manifest_sha256']:
            raise ValueError('preflight source receipt identity mismatch')
        if any((path.exists() or path.is_symlink()) if stamp is None else signature(path) != stamp for path, stamp in self.files):
            raise ValueError('source tree changed after owned preflight')
        return self.evidence


@dataclass(frozen=True)
class CompleteReplayValidation:
    identity: dict
    checkpoint_sha256: str
    point_count: int
    start_boundary: int
    end_boundary: int
    relative_files: tuple
    files: tuple = ()


_complete = {}


def adopt_complete(receipts, stable_base):
    """After verified installation under campaign ownership, bind exact inodes."""
    from dataclasses import replace
    _complete.clear()
    for relative_root, receipt in receipts.items():
        root = Path(stable_base) / relative_root
        stamps = tuple((root / path, signature(root / path)) for path in receipt.relative_files)
        _complete[str(root)] = replace(receipt, files=stamps)


def consume_complete(store, lease):
    lease.validate(store.root)
    receipt = _complete.pop(str(store.root), None)
    if receipt is None:
        return False
    if (receipt.identity != store.identity or receipt.start_boundary != store.start_boundary
            or receipt.end_boundary != store.end_boundary or any(signature(path) != stamp for path, stamp in receipt.files)):
        return False  # changed: caller performs full validation again
    store.previous_sha = receipt.checkpoint_sha256
    store.previous_boundary = store.end_boundary
    store.point_count = receipt.point_count
    store._complete = True
    store._validated_spool_identity = {**store.identity, "final_replay_checkpoint_sha256": receipt.checkpoint_sha256,
        "point_count": receipt.point_count, "first_boundary": receipt.start_boundary, "last_boundary": receipt.end_boundary}
    return True
