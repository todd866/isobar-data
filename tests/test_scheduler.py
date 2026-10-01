"""Cadence, catch-up, token deferral, and an unchanged Bureau pull."""

import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from isobar_data.bom import sync_obs
from isobar_data.config import ConfigError, load_config
from isobar_data.grid import LEAD_HOURS, cycle_pair
from isobar_data.http import Stats
from isobar_data.ledger import source_label
from isobar_data.openmeteo import meta_decision
from isobar_data.scheduler import (
    CHUNK_GAP,
    Source,
    build_sources,
    is_due,
    open_meteo_runs,
    run_sources,
    select_catchup,
)
from isobar_data.tokens import Bucket, Window

UTC = timezone.utc


def test_catchup_keeps_two_runs_and_open_meteo_keeps_the_current_init():
    assert select_catchup(["c", "b", "a"], set()) == ["c", "b"]
    assert select_catchup(["c", "b", "a"], {"c"}) == ["b"]
    current = "2026-09-26T00:00:00Z"
    assert open_meteo_runs(current, None) == [current]
    assert open_meteo_runs(current, current) == []
    assert open_meteo_runs(current, "2026-09-25T18:00:00Z") == [current]
    assert CHUNK_GAP == timedelta(minutes=1)


def test_source_cadences_and_the_default_station_list():
    config = load_config()
    by_id = {source.id: source for source in build_sources(config)}
    assert by_id["bom-obs"].cadence == timedelta(minutes=15)
    assert by_id["aviation-sigmet"].cadence == timedelta(minutes=5)
    assert by_id["aviation-metar"].cadence == timedelta(minutes=5)
    assert by_id["ourairports"].cadence == timedelta(days=7)
    assert len(config.surface) == 6
    assert len(config.marine_points) == 2
    assert len(config.ensemble) == 2
    assert [item.icao for item in config.aerodromes] == ["YPPH", "YSSY"]
    spots = {point.id: point.onshore_from_deg for point in config.spots}
    assert spots["cottesloe"] == 270
    assert spots["safety-bay"] == 235
    assert config.kite.speed_min_kt == 15
    assert config.kite.speed_max_kt == 30
    assert config.kite.excluded_shore == ("offshore",)
    now = datetime(2026, 9, 26, tzinfo=UTC)
    assert not is_due(
        {"last_success": "2026-09-26T00:00:00Z"},
        now + timedelta(minutes=10),
        timedelta(minutes=15),
        always=False,
    )
    assert is_due({}, now, timedelta(minutes=15), always=False)


def test_a_ninth_surface_point_is_rejected(tmp_path):
    text = Path("config/isobar.toml").read_text()
    extra = ""
    for index in range(3):
        extra += f"""
[[surface]]
id = "extra-{index}"
latitude = -32.0
longitude = 115.0
"""
    path = tmp_path / "isobar.toml"
    path.write_text(text + extra)
    try:
        load_config(path)
    except ConfigError as exc:
        assert "surface points" in str(exc)
    else:
        raise AssertionError("nine surface points exceed the cap")


def test_low_free_space_does_not_fetch(tmp_path, monkeypatch):
    called = {"n": 0}

    def run(_ctx):
        called["n"] += 1
        return {"ok": True, "complete": True}

    monkeypatch.setattr("isobar_data.scheduler.volume_free_bytes", lambda _path: 1, raising=False)
    monkeypatch.setattr(
        "isobar_data.scheduler.build_sources",
        lambda _config: [Source("probe", "Probe", timedelta(0), 0, None, False, True, run)],
    )
    from isobar_data.scheduler import run_once

    report = run_once(tmp_path, load_config(), datetime(2026, 9, 26, 8, tzinfo=UTC))
    assert called["n"] == 0
    assert report["http_requests"] == 0
    assert any(item.get("skipped") == "volume free space is below 20 GB" for item in report["sources"])


def test_an_empty_bucket_defers_without_running(tmp_path):
    called = {"n": 0}

    def run(_ctx):
        called["n"] += 1
        return {"ok": True, "complete": True}

    now = datetime(2026, 9, 26, 8, tzinfo=UTC)
    source = Source("heavy", "Heavy", timedelta(0), 11, "tiny", False, False, run)
    ctx = {
        "now": now,
        "state": {},
        "buckets": {"tiny": Bucket("tiny", (Window("minute", 10, 60),))},
        "root": tmp_path,
    }
    reports = run_sources([source], ctx)
    assert called["n"] == 0
    assert reports[0]["skipped"] == "bucket"


def test_second_ftp_sync_does_not_download(tmp_path):
    payload = json.dumps({
        "observations": {
            "header": [{"ID": "IDW60910", "name": "Swanbourne", "wmo_id": "94614"}],
            "data": [{
                "wmo": 94614,
                "name": "Swanbourne",
                "aifstime_utc": "20260926050000",
                "lat": -31.956,
                "lon": 115.7619,
                "vis_km": "-",
                "cloud_base_m": None,
                "history_product": "IDW60910",
            }],
        }
    }).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        info = tarfile.TarInfo("swanbourne.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    body = archive.getvalue()

    class Session:
        def __init__(self):
            self.retr_count = 0

        def size(self, _name):
            return len(body)

        def mdtm(self, _name):
            return "20260926050000"

        def retr(self, _name):
            self.retr_count += 1
            return body

        def close(self):
            return None

    session = Session()
    state = {}
    first = sync_obs(tmp_path, session, ("IDW60910.tgz",), state)
    second = sync_obs(tmp_path, session, ("IDW60910.tgz",), state)
    assert first["downloaded"] == 1
    assert second["downloaded"] == 0
    assert second["skipped"] == 1
    assert session.retr_count == 1


def test_meta_gate_waits_ten_minutes_and_skips_a_stored_init():
    available = datetime(2026, 9, 26, 7, 22, tzinfo=UTC)
    init = datetime(2026, 9, 26, 0, tzinfo=UTC)
    meta = {
        "last_run_initialisation_time": init.timestamp(),
        "last_run_availability_time": available.timestamp(),
    }
    early = meta_decision(meta, available + timedelta(minutes=5), None)
    assert early.fetch_body is False
    assert early.not_before == available + timedelta(minutes=10)
    ready = meta_decision(meta, available + timedelta(minutes=11), None)
    assert ready.fetch_body is True
    stored = meta_decision(meta, available + timedelta(minutes=11), "2026-09-26T00:00:00Z")
    assert stored.fetch_body is False


def test_status_label_accepts_a_joined_run_and_an_http_date():
    from isobar_data.ledger import parse_iso

    joined = parse_iso("2026-09-26T00:00:00Z|2026-09-26T12:00:00Z")
    assert joined == datetime(2026, 9, 26, 12, tzinfo=UTC)
    http_date = parse_iso("Sat, 26 Sep 2026 01:54:01 GMT")
    assert http_date == datetime(2026, 9, 26, 1, 54, 1, tzinfo=UTC)


def test_status_label_uses_the_run_hour():
    now = datetime(2026, 9, 26, 0, tzinfo=UTC)
    run = datetime(2026, 9, 25, 18, tzinfo=UTC)
    assert source_label("ECMWF", run, now, cycle=True) == "ECMWF 18Z · 6 h ago"
    assert source_label("BoM obs", now - timedelta(minutes=12), now, cycle=False) == "BoM obs · 12 min ago"


def test_ecmwf_cycles_are_the_latest_00z_and_12z():
    now = datetime(2026, 9, 26, 7, 40, tzinfo=UTC)
    first, second = cycle_pair(now)
    assert first == datetime(2026, 9, 26, 0, tzinfo=UTC)
    assert second == datetime(2026, 9, 25, 12, tzinfo=UTC)
    assert len(LEAD_HOURS) == 33
    assert LEAD_HOURS[0] == 0
    assert LEAD_HOURS[-1] == 96
