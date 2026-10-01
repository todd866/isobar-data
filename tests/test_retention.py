"""Retention ages raw bytes and grid valid times, and leaves current products."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from isobar_data.aviation_feed import _history
from isobar_data.normalise import connect_obs, insert_obs
from isobar_data.retain import ensure_root, retain
from isobar_data.storage import directory_size

UTC = timezone.utc
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def _touch(path: Path, when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def _grid(root: Path, variable: str, valid: str) -> Path:
    path = root / "products" / "grids" / "ecmwf_ifs025" / "20260906T00Z" / variable / f"{valid}.f16"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00")
    path.with_suffix(".json").write_text("{}\n")
    return path


def test_retention_thins_old_grids_and_keeps_current_products(tmp_path):
    root = tmp_path / "isobar"
    ensure_root(root)
    old_mslp = _grid(root, "mslp", "20260906T00Z")
    old_t850 = _grid(root, "t850", "20260906T00Z")
    old_t2m = _grid(root, "t2m", "20260906T00Z")
    recent = _grid(root, "mslp", "20260925T12Z")
    thin_old = root / "products" / "grids" / "ecmwf_ifs025" / "thin" / "mslp" / "20200101T00Z.f16"
    thin_old.parent.mkdir(parents=True, exist_ok=True)
    thin_old.write_bytes(b"\x00\x00")

    _touch(root / "raw" / "bom" / "IDW60910.tgz" / "old.tgz", NOW - timedelta(days=10))
    _touch(root / "raw" / "open-meteo" / "body.json", NOW - timedelta(days=20))
    fresh = root / "raw" / "open-meteo" / "fresh.json"
    _touch(fresh, NOW - timedelta(hours=1))
    kite = root / "products" / "kite" / "cottesloe.json"
    _touch(kite, NOW - timedelta(days=40))
    attribution = root / "attribution.json"
    _touch(attribution, NOW - timedelta(days=40))
    ensemble_latest = root / "products" / "ensemble" / "swanbourne.json"
    _touch(ensemble_latest, NOW - timedelta(days=400))
    ensemble_dated = root / "products" / "ensemble" / "swanbourne" / "20200101T00Z.json"
    _touch(ensemble_dated, NOW - timedelta(days=400))

    connection = connect_obs(root / "products" / "obs" / "obs.sqlite")
    row = {
        "wmo": 94614,
        "aifstime_utc": "20200101000000",
        "product_id": "IDW60910",
        "name": "Swanbourne",
        "wind_spd_kt": 1,
    }
    recent_row = dict(row, aifstime_utc="20260926050000")
    insert_obs(connection, [row, recent_row])
    connection.close()

    summary = retain(root, NOW)
    assert any("mslp" in item for item in summary["removed"])
    assert not old_mslp.exists()
    assert not old_t850.exists()
    assert not old_t2m.exists()
    assert recent.is_file()
    thin_mslp = root / "products" / "grids" / "ecmwf_ifs025" / "thin" / "mslp" / "20260906T00Z.f16"
    thin_t850 = root / "products" / "grids" / "ecmwf_ifs025" / "thin" / "t850" / "20260906T00Z.f16"
    assert thin_mslp.is_file()
    assert thin_t850.is_file()
    assert not (root / "products" / "grids" / "ecmwf_ifs025" / "thin" / "t2m").exists()
    assert not thin_old.exists()
    assert not (root / "raw" / "bom").exists() or not any((root / "raw" / "bom").rglob("*"))
    assert not (root / "raw" / "open-meteo" / "body.json").exists()
    assert fresh.is_file()
    assert kite.is_file()
    assert attribution.is_file()
    assert ensemble_latest.is_file()
    assert not ensemble_dated.exists()
    connection = connect_obs(root / "products" / "obs" / "obs.sqlite")
    stamps = [item[0] for item in connection.execute("SELECT aifstime_utc FROM obs")]
    connection.close()
    assert stamps == ["20260926050000"]
    assert summary["obs_rows"] == 1


def test_ecmwf_raw_expires_after_48_hours_and_partials_after_one_hour(tmp_path):
    from isobar_data.retain import FREE_SPACE_FLOOR, STORE_BUDGET_BYTES

    assert STORE_BUDGET_BYTES == 8 * 1024**3
    assert FREE_SPACE_FLOOR == 20 * 1024**3
    root = tmp_path / "isobar"
    ensure_root(root)
    expired = root / "raw" / "ecmwf" / "old.grib"
    kept = root / "raw" / "ecmwf" / "fresh.grib"
    stale_partial = root / "raw" / "ecmwf" / "step.grib.partial"
    fresh_partial = root / "raw" / "open-meteo" / "body.json.partial"
    _touch(expired, NOW - timedelta(hours=49))
    _touch(kept, NOW - timedelta(hours=47))
    _touch(stale_partial, NOW - timedelta(hours=2))
    _touch(fresh_partial, NOW - timedelta(minutes=10))
    retain(root, NOW)
    assert not expired.exists()
    assert kept.is_file()
    assert not stale_partial.exists()
    assert fresh_partial.is_file()


def test_aviation_history_is_pruned(tmp_path):
    root = tmp_path / "isobar"
    ensure_root(root)
    connection = _history(root / "products" / "aviation" / "history.sqlite")
    connection.execute(
        "INSERT INTO metar (icao, obs_time, raw, visibility_m, cloud_base_ft, ceiling_ft) VALUES (?, ?, ?, ?, ?, ?)",
        ("YPPH", "2020-01-01T00:00:00Z", "old", None, None, None),
    )
    connection.execute(
        "INSERT INTO metar (icao, obs_time, raw, visibility_m, cloud_base_ft, ceiling_ft) VALUES (?, ?, ?, ?, ?, ?)",
        ("YPPH", "2026-09-26T00:00:00Z", "new", None, None, None),
    )
    connection.commit()
    connection.close()
    retain(root, NOW)
    connection = _history(root / "products" / "aviation" / "history.sqlite")
    stamps = [row[0] for row in connection.execute("SELECT obs_time FROM metar ORDER BY obs_time")]
    connection.close()
    assert stamps == ["2026-09-26T00:00:00Z"]


def test_byte_cap_drops_raw_files_and_keeps_the_published_run(tmp_path):
    root = tmp_path / "isobar"
    ensure_root(root)
    published = root / "products" / "grids" / "ecmwf_ifs025" / "runs" / "20260926T00Z" / "mslp"
    published.mkdir(parents=True)
    (published / "20260926T00Z.f16").write_bytes(b"p" * 4000)
    pointer = root / "products" / "grids" / "ecmwf_ifs025" / "current.json"
    pointer.write_text('{"latest": "20260926T00Z", "runs": ["20260926T00Z"]}\n')
    raw = root / "raw" / "ecmwf" / "big.grib"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"r" * 4000)
    retain(root, NOW, budget_bytes=5000)
    assert not raw.exists()
    assert (published / "20260926T00Z.f16").is_file()
    assert directory_size(root) <= 5000


def test_unsatisfiable_cap_is_reported_and_blocks_a_large_fetch(tmp_path, monkeypatch):
    """Protected files over the cap stay, and ECMWF does not add another object."""
    import json
    from datetime import timedelta

    from isobar_data.config import load_config
    from isobar_data.scheduler import Source, run_once

    root = tmp_path / "isobar"
    ensure_root(root)
    published = root / "products" / "grids" / "ecmwf_ifs025" / "runs" / "20260926T00Z" / "mslp"
    published.mkdir(parents=True)
    (published / "20260926T00Z.f16").write_bytes(b"p" * 4000)
    pointer = root / "products" / "grids" / "ecmwf_ifs025" / "current.json"
    pointer.write_text('{"latest": "20260926T00Z", "runs": ["20260926T00Z"]}\n')
    raw = root / "raw" / "ecmwf" / "old.grib"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"r" * 500)
    monkeypatch.setattr("isobar_data.retain.STORE_BUDGET_BYTES", 3000)
    monkeypatch.setattr("isobar_data.scheduler.STORE_BUDGET_BYTES", 3000)
    summary = retain(root, NOW, budget_bytes=3000)
    assert summary.get("within_budget") is False
    assert not raw.exists()
    assert (published / "20260926T00Z.f16").is_file()
    assert directory_size(root) > 3000

    called = {"n": 0}

    def run(_ctx):
        called["n"] += 1
        return {"ok": True, "complete": True, "detail": "downloaded"}

    monkeypatch.setattr(
        "isobar_data.scheduler.build_sources",
        lambda _config: [Source("ecmwf-open-data", "ECMWF", timedelta(0), 0, None, True, False, run)],
    )
    run_once(root, load_config(), NOW)
    assert called["n"] == 0
    status = json.loads((root / "status.json").read_text())
    disk = status.get("disk") or {}
    assert disk.get("within_budget") is False
    assert "8 GB" in disk.get("detail", "")


def test_ecmwf_does_not_start_when_one_object_would_cross_the_cap(tmp_path, monkeypatch):
    import json
    from datetime import timedelta

    from isobar_data.config import load_config
    from isobar_data.grid import archive_run
    from isobar_data.scheduler import Source, run_once

    root = tmp_path / "isobar"
    ensure_root(root)
    published = root / "products" / "grids" / "ecmwf_ifs025" / "runs" / "20260926T00Z" / "mslp"
    published.mkdir(parents=True)
    (published / "20260926T00Z.f16").write_bytes(b"p" * 4000)
    pointer = root / "products" / "grids" / "ecmwf_ifs025" / "current.json"
    pointer.write_text('{"latest": "20260926T00Z", "runs": ["20260926T00Z"]}\n')
    monkeypatch.setattr("isobar_data.retain.STORE_BUDGET_BYTES", 8000)
    monkeypatch.setattr("isobar_data.retain.LARGE_FETCH_RESERVE", 5000)
    monkeypatch.setattr("isobar_data.scheduler.STORE_BUDGET_BYTES", 8000)
    monkeypatch.setattr("isobar_data.scheduler.LARGE_FETCH_RESERVE", 5000, raising=False)
    downloads = {"n": 0}

    def fetch(*_args, **_kwargs):
        downloads["n"] += 1
        raise RuntimeError("download")

    monkeypatch.setattr("isobar_data.grid.fetch_bytes", fetch)
    try:
        archive_run(root, NOW, consume=lambda: True)
    except Exception:
        pass
    assert downloads["n"] == 0

    called = {"small": 0, "large": 0}

    def small(_ctx):
        called["small"] += 1
        return {"ok": True, "complete": True, "detail": ""}

    def large(_ctx):
        called["large"] += 1
        return {"ok": True, "complete": True, "detail": "downloaded"}

    monkeypatch.setattr(
        "isobar_data.scheduler.build_sources",
        lambda _config: [
            Source("kite", "Kite", timedelta(0), 0, None, False, True, small),
            Source("ecmwf-open-data", "ECMWF", timedelta(0), 0, None, True, False, large),
        ],
    )
    run_once(root, load_config(), NOW)
    assert called["small"] == 1
    assert called["large"] == 0
    status = json.loads((root / "status.json").read_text())
    assert "8 GB" in (status.get("disk") or {}).get("detail", "")


def test_log_rotation_keeps_three_backups(tmp_path):
    from isobar_data.retain import rotate_log

    log = tmp_path / "isobar-data.log"
    log.write_bytes(b"a" * 80)
    assert rotate_log(log, max_bytes=50, backups=3) is True
    assert (tmp_path / "isobar-data.log.1").read_bytes() == b"a" * 80
    assert not log.exists()
    log.write_bytes(b"b" * 80)
    rotate_log(log, max_bytes=50, backups=3)
    assert (tmp_path / "isobar-data.log.1").read_bytes() == b"b" * 80
    assert (tmp_path / "isobar-data.log.2").read_bytes() == b"a" * 80
