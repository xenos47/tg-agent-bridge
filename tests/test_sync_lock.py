from pathlib import Path

from tgbridge.sync.lock import sync_lock_path, try_acquire_sync_lock


def test_sync_lock_path_sits_next_to_session(tmp_path: Path) -> None:
    assert sync_lock_path(tmp_path / "tgq.session") == tmp_path / "sync.lock"
    assert sync_lock_path(tmp_path / "tgq") == tmp_path / "sync.lock"


def test_overlapping_lock_yields_false(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    with try_acquire_sync_lock(lock) as first:
        assert first is True
        with try_acquire_sync_lock(lock) as second:
            assert second is False
    with try_acquire_sync_lock(lock) as third:
        assert third is True
