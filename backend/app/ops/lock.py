"""Exclusive flock so two paper processes cannot share one SQLite directory."""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
from typing import IO, TextIO


def instance_lock_path(database_url: str, directory: Path | None = None) -> Path:
    digest = hashlib.sha1(database_url.encode("utf-8")).hexdigest()[:12]
    return (directory or Path("var")) / f"paper.{digest}.lock"


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | TextIO | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def unlock(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
