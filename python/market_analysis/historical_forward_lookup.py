"""Disposable disk lookup for candidate-independent forward computations."""
from contextlib import AbstractContextManager
from pathlib import Path
import sqlite3
import tempfile

from .historical_replay_runtime import _canonical_bytes, _read_json_bytes


class ForwardLookup(AbstractContextManager):
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="historical-forward-")
        self.connection = sqlite3.connect(str(Path(self.directory.name) / "labels.sqlite"))
        self.connection.execute("CREATE TABLE labels (kind TEXT, identity TEXT, decision INTEGER, horizon INTEGER, value BLOB, PRIMARY KEY (kind, identity, decision, horizon))")
        return self

    def get(self, kind, identity, decision, horizon, compute):
        from .historical_study_runtime import decode, encode
        key = (kind, identity, decision, horizon)
        row = self.connection.execute(
            "SELECT value FROM labels WHERE kind=? AND identity=? AND decision=? AND horizon=?", key).fetchone()
        if row is not None:
            return decode(_read_json_bytes(row[0]))
        value = compute()
        self.connection.execute("INSERT INTO labels VALUES (?, ?, ?, ?, ?)",
                                (*key, _canonical_bytes(encode(value))))
        return value

    def __exit__(self, *exc):
        self.connection.close()
        self.directory.cleanup()
