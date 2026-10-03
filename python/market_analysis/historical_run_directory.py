"""Exclusive local period ownership and cleanup of abandoned owned temporaries."""
from contextlib import contextmanager
import fcntl
from pathlib import Path
import shutil


@contextmanager
def owned_run_directory(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".writer.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("historical period already has an active local writer") from exc
        try:
            # Only runtime-created names under this owned period; requests/jobs
            # from failed stages remain available for diagnosis and retry.
            for path in root.glob(".study-points-*"):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            for directory in (root, root / "post-replay"):
                if not directory.exists():
                    continue
                for pattern in (".*.tmp", ".shared-v1-*", ".*.records-*"):
                    for path in directory.glob(pattern):
                        if path.is_file() and not path.is_symlink():
                            path.unlink()
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
