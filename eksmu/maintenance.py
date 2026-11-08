"""Local instance leases for cooperating servers and offline maintenance (Unix)."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import time

RESTORING = '.eksmu-restore-incomplete'


def local_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('Symlinked instance or backup paths are not supported')
    return path


class InstanceLock:
    """Never unlink the lock: replacing its inode would defeat other holders."""
    def __init__(self, database: str | Path, *, exclusive: bool = False,
                 wait_seconds=0, suffix='.maintenance.lock'):
        import fcntl
        database = local_path(database)
        if (database.parent / RESTORING).exists():
            raise ValueError('Instance restore is incomplete; use another destination')
        self.fd = None
        fd = os.open(str(database) + suffix,
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Instance lock must be a regular file')
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.01)
        except BlockingIOError as exc:
            os.close(fd)
            raise ValueError('Instance is in use; stop the service before offline maintenance') from exc
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class RequestLock(InstanceLock):
    """Serialize HTTP writes, including authorization and sidecar mutations."""
    def __init__(self, database):
        from .store import Problem
        try:
            super().__init__(database, exclusive=True, wait_seconds=5, suffix='.requests.lock')
        except ValueError as exc:
            raise Problem('写入正在处理中，请稍后重试', 503) from exc
