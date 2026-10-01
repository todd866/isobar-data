from __future__ import annotations

from pathlib import Path

import pytest

from isobar_data import retain
from isobar_data.scheduler import fetch_blocked


def test_default_floor_remains_20_gb(monkeypatch):
    monkeypatch.delenv("ISOBAR_FREE_SPACE_FLOOR_GB", raising=False)
    assert retain.free_space_floor_bytes() == 20 * 1024**3
    assert retain.free_space_floor_label() == "20"


def test_override_is_shared_by_scheduler_and_retention(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ISOBAR_FREE_SPACE_FLOOR_GB", "2.5")
    monkeypatch.setattr("isobar_data.scheduler.volume_free_bytes", lambda _root: int(2.4 * 1024**3))
    monkeypatch.setattr("isobar_data.retain.volume_free_bytes", lambda _root: int(2.4 * 1024**3))
    assert fetch_blocked(tmp_path) == "volume free space is below 2.5 GB"
    assert retain.large_fetch_block(tmp_path) == "volume free space is below 2.5 GB"


@pytest.mark.parametrize("raw", ["", "1.99", "nan", "inf", "-2", "two"])
def test_invalid_override_is_rejected(raw, monkeypatch):
    monkeypatch.setenv("ISOBAR_FREE_SPACE_FLOOR_GB", raw)
    with pytest.raises(ValueError, match="ISOBAR_FREE_SPACE_FLOOR_GB"):
        retain.free_space_floor_bytes()


def test_scheduler_guard_rejects_invalid_override_before_disk_check(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ISOBAR_FREE_SPACE_FLOOR_GB", "nope")
    monkeypatch.setattr("isobar_data.scheduler.volume_free_bytes", lambda _root: pytest.fail("disk check ran"))
    with pytest.raises(ValueError, match="ISOBAR_FREE_SPACE_FLOOR_GB"):
        fetch_blocked(tmp_path)


def test_retention_guard_rejects_invalid_override_before_disk_check(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ISOBAR_FREE_SPACE_FLOOR_GB", "nope")
    monkeypatch.setattr("isobar_data.retain.volume_free_bytes", lambda _root: pytest.fail("disk check ran"))
    with pytest.raises(ValueError, match="ISOBAR_FREE_SPACE_FLOOR_GB"):
        retain.large_fetch_block(tmp_path)
