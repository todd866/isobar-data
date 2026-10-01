"""Open-Meteo variable lists and upper-air normalisation (fixture-only)."""

from datetime import datetime, timedelta, timezone

import httpx

from isobar_data.http import Http, Stats
from isobar_data.kite_job import daylight_from_point
from isobar_data.openmeteo import (
    SURFACE_DAILY,
    SURFACE_HOURLY,
    SURFACE_HOURS,
    UPPER_LEVELS_HPA,
    _hourly_profile,
    _upper_levels,
    fetch_job,
    normalise_point,
    upper_hourly,
)
from isobar_data.policy import assert_http_allowed
from isobar_data.publish import read_published
from isobar_data.scheduler import _surface
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


def test_gmt_hourly_times_stay_as_the_api_sent_them():
    body = {
        "latitude": -31.9,
        "longitude": 115.9,
        "timezone": "GMT",
        "hourly": {
            "time": ["2026-09-26T00:00", "2026-09-26T03:00"],
            "temperature_850hPa": [1.0, 2.0],
        },
    }
    product = normalise_point(
        body,
        kind="upper",
        point_id="YPPH",
        run="2026-09-26T00:00:00Z",
        native_step=3,
        model="ecmwf_ifs025",
    )
    assert product["time"] == ["2026-09-26T00:00", "2026-09-26T03:00"]
    assert "timezone" not in product


def test_surface_request_is_seven_local_days_and_hourly_stays_gmt(tmp_path):
    from urllib.parse import parse_qs, urlparse

    from isobar_data.config import load_config
    from isobar_data.http import Http, Stats

    assert SURFACE_HOURS == 168
    init = datetime(2026, 10, 1, 0, tzinfo=UTC)
    available = datetime(2026, 10, 1, 7, 22, tzinfo=UTC)
    meta = {
        "last_run_initialisation_time": init.timestamp(),
        "last_run_availability_time": available.timestamp(),
    }
    perth_daily = {
        "time": ["2026-10-02"],
        "sunrise": ["2026-10-02T05:47"],
        "sunset": ["2026-10-02T18:19"],
        "temperature_2m_max": [24.2],
        "temperature_2m_min": [13.4],
        "precipitation_sum": [1.2],
        "precipitation_hours": [2.0],
        "weather_code": [3],
        "wind_speed_10m_max": [18.0],
        "wind_gusts_10m_max": [28.0],
        "wind_direction_10m_dominant": [210],
    }
    sydney_daily = {
        "time": ["2026-10-04"],
        "sunrise": ["2026-10-04T05:28"],
        "sunset": ["2026-10-04T18:00"],
        "temperature_2m_max": [22.0],
        "temperature_2m_min": [15.0],
        "precipitation_sum": [0.0],
        "precipitation_hours": [0.0],
        "weather_code": [1],
        "wind_speed_10m_max": [12.0],
        "wind_gusts_10m_max": [16.0],
        "wind_direction_10m_dominant": [40],
    }

    def location(zone, times, daily):
        # Open-Meteo writes every local time at one fixed offset (the offset at
        # request time), even across a daylight-saving change.
        return {
            "latitude": -32.0,
            "longitude": 115.7,
            "elevation": 0,
            "timezone": zone,
            "utc_offset_seconds": 36000 if zone == "Australia/Sydney" else 28800,
            "hourly": {
                "time": times,
                "temperature_2m": [20.0] * len(times),
            },
            "hourly_units": {"temperature_2m": "°C"},
            "daily": daily,
            "daily_units": {
                "temperature_2m_max": "°C",
                "precipitation_sum": "mm",
                "wind_speed_10m_max": "kn",
            },
        }

    config = load_config()
    bodies = []
    for point in config.surface:
        if point.id == "yssy":
            bodies.append(location(
                "Australia/Sydney",
                ["2026-10-04T01:00", "2026-10-04T02:00", "2026-10-04T03:00"],
                sydney_daily,
            ))
        elif point.id == "cottesloe":
            bodies.append(location(
                "Australia/Perth",
                ["2026-10-02T00:00", "2026-10-02T01:00"],
                perth_daily,
            ))
        else:
            bodies.append(location(
                "Australia/Perth",
                ["2026-10-02T00:00"],
                perth_daily,
            ))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("meta.json"):
            return httpx.Response(200, json=meta)
        seen.append(str(request.url))
        return httpx.Response(200, json=bodies)

    now = available + timedelta(minutes=11)
    http = Http(
        standard_buckets(),
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: now,
    )
    result = _surface({
        "http": http,
        "root": tmp_path,
        "state": {},
        "now": now,
        "config": config,
    })
    http.close()
    assert result["ok"] is True
    assert len(seen) == 1
    assert_http_allowed(seen[0])
    query = parse_qs(urlparse(seen[0]).query)
    assert query["forecast_hours"] == [str(SURFACE_HOURS)]
    assert query["timezone"] == ["auto"]
    assert query["models"] == ["ecmwf_ifs"]
    assert query["daily"] == [",".join(SURFACE_DAILY)]
    assert query["hourly"] == [",".join(SURFACE_HOURLY)]

    perth = read_published(tmp_path, "points/ecmwf_ifs", "cottesloe.json")
    sydney = read_published(tmp_path, "points/ecmwf_ifs", "yssy.json")
    assert perth["schema_version"] == 1
    assert perth["time"] == ["2026-10-01T16:00", "2026-10-01T17:00"]
    assert perth["timezone"] == "Australia/Perth"
    assert perth["daily"]["time"] == ["2026-10-02"]
    assert perth["daily"]["sunrise"] == ["2026-10-02T05:47+08:00"]
    assert perth["daily"]["sunset"] == ["2026-10-02T18:19+08:00"]
    assert perth["daily"]["temperature_2m_max"] == [24.2]
    assert perth["daily"]["temperature_2m_min"] == [13.4]
    assert perth["daily"]["precipitation_sum"] == [1.2]
    assert perth["daily"]["precipitation_hours"] == [2.0]
    assert perth["daily"]["weather_code"] == [3]
    assert perth["daily"]["wind_speed_10m_max"] == [18.0]
    assert perth["daily"]["wind_gusts_10m_max"] == [28.0]
    assert perth["daily"]["wind_direction_10m_dominant"] == [210]
    rise, settle = daylight_from_point(perth)[0]
    assert rise == datetime(2026, 10, 1, 21, 47, tzinfo=UTC)
    assert settle == datetime(2026, 10, 2, 10, 19, tzinfo=UTC)

    assert sydney["time"] == ["2026-10-03T15:00", "2026-10-03T16:00", "2026-10-03T17:00"]
    assert sydney["timezone"] == "Australia/Sydney"
    assert sydney["daily"]["time"] == ["2026-10-04"]
    # 05:28 at the fixed +10:00 offset is 06:28 daylight time; no hour repeats.
    assert sydney["daily"]["sunrise"] == ["2026-10-04T05:28+10:00"]
    assert sydney["daily"]["temperature_2m_max"] == [22.0]
    assert sydney["daily"]["wind_gusts_10m_max"] == [16.0]
