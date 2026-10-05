"""Shared-descriptor period ownership and cleanup of owned temporaries."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat


class RunDirectoryLease:
    """Borrow the acquired open-file description; never unlock it explicitly."""
    def __init__(self, descriptor, root):
        self.descriptor = descriptor
        self.root = Path(root).resolve()

    def validate(self, root):
        if (type(self.descriptor) is not int or self.descriptor < 0
                or Path(root).resolve() != self.root):
            raise ValueError("missing or invalid historical worker directory lease")
        try:
            held = os.fstat(self.descriptor)
            expected = (self.root / ".writer.lock").stat()
            if (not stat.S_ISREG(held.st_mode)
                    or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)
                    or (fcntl.fcntl(self.descriptor, fcntl.F_GETFL) & os.O_ACCMODE) != os.O_RDWR):
                raise ValueError("historical worker lease does not match the period lock file")
            # Reaffirm ownership on the inherited description. A separately
            # opened descriptor cannot acquire the parent's exclusive flock.
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("historical worker directory lease is invalid or not exclusive") from exc


@contextmanager
def inherited_run_directory_lease(descriptor, root):
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("historical worker requires an inherited directory lease")
    try:
        os.fstat(descriptor)
    except OSError as exc:
        raise ValueError("historical worker directory lease descriptor is invalid") from exc
    try:
        lease = RunDirectoryLease(descriptor, root)
        lease.validate(root)
        yield lease
    finally:
        # The parent and child share an open-file description. LOCK_UN here
        # would also unlock the other holder; close only this process's fd.
        os.close(descriptor)


@contextmanager
def owned_run_directory(root, *, cleanup=True):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".writer.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("historical period already has an active local writer") from exc
        lease = RunDirectoryLease(lock.fileno(), root)
        lease.validate(root)
        if cleanup:
            cleanup_owned_temporaries(root, lease)
        yield lease
        # File context closes the parent's fd even on cancellation. Inherited
        # worker fds retain the exclusive lock until they too are closed.


def cleanup_owned_temporaries(root, lease):
    """Unlink only established runtime temporary names under exclusive ownership.

    NamedTemporaryFile/mkdtemp use eight lowercase/digit/underscore characters.
    Removing an abandoned publication alias preserves its published destination.
    Never acquire a second lock descriptor or follow a directory/file symlink.
    """
    root = Path(root)
    lease.validate(root)
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("owned temporary cleanup requires real directory paths")
    for path in root.glob(".study-points-*"):
        if (re.fullmatch(r"\.study-points-[a-z0-9_]{8}", path.name)
                and not path.is_symlink() and path.is_dir()):
            # rmtree removes child symlinks themselves, never their targets.
            shutil.rmtree(path)
    patterns = (r"\..+\.[a-z0-9_]{8}\.tmp", r"\.shared-v1-[a-z0-9_]{8}",
                r"\..+\.records-[a-z0-9_]{8}")
    for directory in (root, root / "post-replay", root / "chunks", root / "states"):
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.iterdir():
            if (any(re.fullmatch(pattern, path.name) for pattern in patterns)
                    and stat.S_ISREG(path.lstat().st_mode)):
                path.unlink()
