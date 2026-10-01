"""Open-Meteo variable lists and upper-air normalisation (fixture-only)."""

from datetime import datetime, timedelta, timezone

import httpx

from isobar_data.http import Http, Stats
from isobar_data.openmeteo import (
    SURFACE_HOURLY,
    UPPER_LEVELS_HPA,
    _hourly_profile,
    _upper_levels,
    fetch_job,
    normalise_point,
    upper_hourly,
)
from isobar_data.tokens import standard_buckets

UTC = timezone.utc


def test_upper_hourly_requests_rh_and_cloud_at_each_level():
    names = set(upper_hourly())
    for level in UPPER_LEVELS_HPA:
        assert f"relative_humidity_{level}hPa" in names
        assert f"cloud_cover_{level}hPa" in names
        assert f"wind_speed_{level}hPa" in names
        assert f"wind_direction_{level}hPa" in names
        assert f"geopotential_height_{level}hPa" in names
        assert f"temperature_{level}hPa" in names
        assert f"vertical_velocity_{level}hPa" in names


def test_surface_hourly_includes_mid_and_high_cloud_layers():
    assert "cloud_cover_mid" in SURFACE_HOURLY
    assert "cloud_cover_high" in SURFACE_HOURLY


def test_upper_levels_maps_pct_fields_and_preserves_nulls():
    hourly = {
        "temperature_850hPa": [4.5, 5.0],
        "geopotential_height_850hPa": [1537, 1538],
        "wind_speed_850hPa": [19.4, 20.0],
        "wind_direction_850hPa": [91, 92],
        "relative_humidity_850hPa": [80.0, None],
        "cloud_cover_850hPa": [45, None],
        "vertical_velocity_850hPa": [-0.02, 0.04],
        "temperature_1000hPa": [15.0],
        "geopotential_height_1000hPa": [192],
        "relative_humidity_1000hPa": [90],
        "cloud_cover_1000hPa": [100],
    }
    levels = _upper_levels(hourly)
    assert levels["850"]["temperature_c"] == [4.5, 5.0]
    assert levels["850"]["height_m"] == [1537, 1538]
    assert levels["850"]["wind_speed_kt"] == [19.4, 20.0]
    assert levels["850"]["wind_direction_deg"] == [91, 92]
    assert levels["850"]["relative_humidity_pct"] == [80.0, None]
    assert levels["850"]["cloud_cover_pct"] == [45, None]
    assert levels["850"]["vertical_velocity_ms"] == [-0.02, 0.04]
    assert levels["1000"]["relative_humidity_pct"] == [90]
    assert levels["1000"]["cloud_cover_pct"] == [100]
    assert "wind_speed_kt" not in levels["1000"]


def test_upper_levels_omits_missing_optional_fields():
    hourly = {
        "temperature_500hPa": [-22.4],
        "geopotential_height_500hPa": [5670],
    }
    entry = _upper_levels(hourly)["500"]
    assert entry == {"temperature_c": [-22.4], "height_m": [5670]}
    assert "relative_humidity_pct" not in entry
    assert "cloud_cover_pct" not in entry
    assert "vertical_velocity_ms" not in entry


def test_upper_levels_accepts_high_troposphere_and_lower_stratosphere():
    hourly = {
        "temperature_250hPa": [-48.0],
        "geopotential_height_250hPa": [10300],
        "relative_humidity_250hPa": [35],
        "cloud_cover_250hPa": [2],
        "wind_speed_250hPa": [80],
        "wind_direction_250hPa": [270],
        "vertical_velocity_250hPa": [-0.11],
    }
    entry = _upper_levels(hourly)["250"]
    assert entry["height_m"] == [10300]
    assert entry["wind_speed_kt"] == [80]
    assert entry["vertical_velocity_ms"] == [-0.11]


def test_normalise_point_upper_kind_builds_levels_from_hourly():
    body = {
        "latitude": -31.9,
        "longitude": 115.9,
        "hourly": {
            "time": ["2026-09-26T00:00", "2026-09-26T03:00"],
            "temperature_700hPa": [-4.0, -3.5],
            "geopotential_height_700hPa": [3096, 3097],
            "relative_humidity_700hPa": [12, None],
            "cloud_cover_700hPa": [0, 5],
        },
        "hourly_units": {"temperature_700hPa": "°C"},
    }
    product = normalise_point(
        body,
        kind="upper",
        point_id="YPPH",
        run="2026-09-26T00:00:00Z",
        native_step=3,
        model="ecmwf_ifs025",
    )
    assert product["levels"]["700"]["relative_humidity_pct"] == [12, None]
    assert product["levels"]["700"]["cloud_cover_pct"] == [0, 5]


def test_fetch_job_refetches_when_hourly_profile_expands(tmp_path):
    init = datetime(2026, 9, 26, 0, tzinfo=UTC)
    available = datetime(2026, 9, 26, 7, 22, tzinfo=UTC)
    meta = {
        "last_run_initialisation_time": init.timestamp(),
        "last_run_availability_time": available.timestamp(),
    }
    calls = {"forecast": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("meta.json"):
            return httpx.Response(200, json=meta)
        calls["forecast"] += 1
        return httpx.Response(
            200,
            json={
                "latitude": -32.0,
                "longitude": 115.7,
                "hourly": {"time": ["2026-09-26T00:00"], "temperature_850hPa": [1.0]},
            },
        )

    http = Http(
        standard_buckets(),
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: available + timedelta(minutes=11),
    )
    product_dir = tmp_path / "products" / "points" / "ecmwf_ifs025_upper"
    product_dir.mkdir(parents=True)
    state = {
        "sources": {
            "open-meteo-upper": {
                "watermark": "2026-09-26T00:00:00Z",
                "variables": _hourly_profile(("temperature_850hPa",), ()),
            }
        }
    }
    now = available + timedelta(minutes=11)
    kwargs = dict(
        http=http,
        root=tmp_path,
        state=state,
        now=now,
        job_id="open-meteo-upper",
        endpoint="https://api.open-meteo.com/v1/forecast",
        meta_urls=("https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json",),
        points=[("YPPH", -32.0, 115.7)],
        model="ecmwf_ifs025",
        hours=24,
        days=None,
        cell="nearest",
        wind_kn=True,
        elevation_nan=True,
        native_step=1,
        kind="upper",
        product_dir=product_dir,
    )
    skipped = fetch_job(**kwargs, hourly=("temperature_850hPa",))
    assert skipped["detail"] == "run already stored"
    assert calls["forecast"] == 0

    expanded = upper_hourly()
    assert len(expanded) > 1
    fetched = fetch_job(**kwargs, hourly=expanded)
    assert fetched["detail"] == ""
    assert calls["forecast"] == 1
    assert state["sources"]["open-meteo-upper"]["variables"] == _hourly_profile(expanded, ())
    http.close()
