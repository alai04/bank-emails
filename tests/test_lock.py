"""Single-instance lock tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bank_emails.lock import LockUnavailable, SingleInstanceLock


def test_lock_acquire_and_release(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    lock = SingleInstanceLock(path)
    lock.acquire()
    assert path.read_text(encoding="ascii") == str(os.getpid())
    lock.release()
    assert not path.exists()


def test_live_lock_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    path.write_text(str(os.getpid()), encoding="ascii")
    with pytest.raises(LockUnavailable, match=str(os.getpid())):
        SingleInstanceLock(path).acquire()


def test_stale_lock_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    path.write_text("999999999", encoding="ascii")
    lock = SingleInstanceLock(path)
    lock.acquire()
    assert path.read_text(encoding="ascii") == str(os.getpid())
    lock.release()
