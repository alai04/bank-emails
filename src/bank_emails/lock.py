"""Single-instance process lock."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType


class LockUnavailable(RuntimeError):
    """Raised when another live process already owns the lock."""


class SingleInstanceLock:
    """Create an exclusive lock file and remove it on clean shutdown."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._owner = False

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    pid = 0
                if self._process_is_alive(pid):
                    raise LockUnavailable(f"another instance is running with pid {pid}")
                self.path.unlink(missing_ok=True)
                continue

            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
            self._owner = True
            return
        raise LockUnavailable(f"could not acquire lock {self.path}")

    def release(self) -> None:
        if not self._owner:
            return
        try:
            if self.path.exists():
                pid = self.path.read_text(encoding="ascii").strip()
                if pid == str(os.getpid()):
                    self.path.unlink()
        finally:
            self._owner = False

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
