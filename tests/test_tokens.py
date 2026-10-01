"""Token buckets sit 5% under the published caps."""

from datetime import timedelta

from isobar_data.config import load_config
from isobar_data.derive import chunk_ranges, open_meteo_calls
from isobar_data.openmeteo import (
    FORECAST,
    MARINE_HOURLY,
    SURFACE_DAILY,
    SURFACE_HOURLY,
    upper_hourly,
)
from isobar_data.policy import assert_http_allowed
from isobar_data.scheduler import CHUNK_GAP, build_sources
from isobar_data.tokens import aviation_bucket, open_meteo_bucket


def test_open_meteo_minute_window_rejects_a_grid_sized_weight():
    bucket = open_meteo_bucket()
    assert bucket.try_consume(250, 1_000.0)
    assert not bucket.try_consume(1836, 1_000.0)
    bucket.refund(250)
    assert bucket.used(1_000.0, bucket.windows[0]) == 0


def test_limits_are_five_percent_under_the_published_caps():
    minute, hour, day, month = open_meteo_bucket().windows
    assert (minute.limit, hour.limit, day.limit, month.limit) == (570, 4750, 9500, 285_000)
    assert aviation_bucket().windows[0].limit == 95


def test_open_meteo_call_weight_matches_the_pricing_page():
    assert open_meteo_calls(15, 1, 14, 1) == 1.5
    assert open_meteo_calls(15, 1, 28, 1) == 3.0
    assert open_meteo_calls(10, 1, 2, 1836) == 1836
    assert abs(open_meteo_calls(14, 1, 3, 6) - 8.4) < 1e-9


def test_seven_day_surface_schedule_fits_the_open_meteo_bucket():
    """168 h stays on the 14-day weight plateau. The extra daily names do not."""
    assert SURFACE_DAILY == (
        "sunrise",
        "sunset",
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "precipitation_hours",
        "weather_code",
        "wind_speed_10m_max",
        "wind_gusts_10m_max",
        "wind_direction_10m_dominant",
    )
    variables = len(SURFACE_HOURLY) + len(SURFACE_DAILY)
    assert open_meteo_calls(variables, 1, 168 / 24, 1) == 2.4
    assert open_meteo_calls(variables, 1, 7, 1) == open_meteo_calls(variables, 1, 1, 1)

    # Four ECMWF initialisations a day. The 15-minute cadence only re-reads meta.
    runs = 4
    checks = int(timedelta(days=1) / timedelta(minutes=15))
    by_id = {source.id: source for source in build_sources(load_config())}
    assert by_id["open-meteo-ifs"].cadence == timedelta(minutes=15)
    assert by_id["open-meteo-upper"].cadence == timedelta(minutes=15)
    assert by_id["open-meteo-marine"].cadence == timedelta(minutes=15)
    assert by_id["open-meteo-ensemble"].cadence == timedelta(minutes=15)

    surface_run = open_meteo_calls(variables, 1, 168 / 24, 8)
    upper_day = open_meteo_calls(len(upper_hourly()), 1, 72 / 24, 8) * runs
    marine_day = open_meteo_calls(len(MARINE_HOURLY), 1, 72 / 24, 4) * runs
    ensemble_day = open_meteo_calls(2, 1, 7, 2) * runs
    meta_day = checks * (1 + 1 + 2 + 1)
    total = surface_run * runs + upper_day + marine_day + ensemble_day + meta_day

    windows = {window.name: window for window in open_meteo_bucket().windows}
    assert surface_run <= windows["minute"].limit
    assert surface_run <= windows["hour"].limit
    assert total <= windows["day"].limit
    assert total * 30 <= windows["month"].limit
    assert_http_allowed(FORECAST)


def test_chunks_of_250_leave_a_minute_between_them():
    parts = chunk_ranges(300, 250)
    assert [list(part)[0] for part in parts] == [0, 250]
    assert list(parts[0])[-1] == 249
    assert list(parts[1]) == list(range(250, 300))
    assert CHUNK_GAP == timedelta(minutes=1)
