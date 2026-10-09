"""Stable SQLite snapshots without touching the archive's WAL shared memory."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path


def _signature(path: Path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


@contextmanager
def observation_snapshot(path: Path):
    """Copy a stable DB + WAL pair, then let SQLite read the disposable snapshot.

    mode=ro alone can create/update -shm in the original store. immutable=1
    avoids that but ignores committed WAL rows. Copying the pair between
    unchanged stat signatures preserves WAL rows without either failure mode.
    A busy collector causes a bounded retry and then an explicit unavailable.
    """
    wal = path.with_name(path.name+'-wal')
    for source in (path,wal):
        if source.is_symlink():
            raise ValueError('observation DB/WAL must not be symbolic links')
    with tempfile.TemporaryDirectory(prefix='isobar-archive-obs-') as temporary:
        target = Path(temporary)/'obs.sqlite'
        target_wal = target.with_name(target.name+'-wal')
        for _ in range(3):
            before = (_signature(path),_signature(wal))
            if before[0] is None:
                raise FileNotFoundError('observations database disappeared')
            shutil.copyfile(path,target)
            if before[1] is not None:
                try:
                    shutil.copyfile(wal,target_wal)
                except FileNotFoundError:
                    continue
            else:
                target_wal.unlink(missing_ok=True)
            if before == (_signature(path),_signature(wal)):
                break
        else:
            raise ValueError('observations changed during snapshot; retry the query')
        db = sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            yield db
        finally:
            db.close()
