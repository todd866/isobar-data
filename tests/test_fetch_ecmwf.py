"""Pure tests for the ECMWF crop. No portal and no GRIB decoder."""

from datetime import datetime, timedelta, timezone

import numpy as np

from isobar_data.fetch_ecmwf import (
    accumulation_window,
    chart_times,
    crop_north_up,
    grid_shape,
    index_url,
    lead_hours,
    parse_index,
    product_stem,
    prognosis_times,
    rain_source,
    select_message,
    to_display_units,
)

UTC = timezone.utc


def test_grid_is_the_australian_quarter_degree_window():
    nx, ny = grid_shape()
    assert (nx, ny) == (301, 201)
    assert nx * ny == 60501


def test_product_urls_match_the_open_data_portal():
    run = datetime(2026, 9, 25, 12, tzinfo=UTC)
    stem = product_stem(run, 36)
    assert stem == "20260925/12z/ifs/0p25/oper/20260925120000-36h-oper-fc"
    assert index_url(run, 36).endswith(stem + ".index")
    assert "data.ecmwf.int/forecasts/" in index_url(run, 0)


def test_index_selects_one_byte_range():
    text = "\n".join([
        '{"param":"z","levtype":"sfc","step":"0","_offset":0,"_length":10}',
        '{"param":"msl","levtype":"sfc","step":"0","_offset":10,"_length":20}',
        '{"param":"t","levtype":"pl","levelist":"850","step":"0","_offset":30,"_length":40}',
        '{"param":"t","levtype":"pl","levelist":"500","step":"0","_offset":70,"_length":5}',
        '{"param":"tp","levtype":"sfc","step":"0","_offset":80,"_length":8}',
    ])
    rows = parse_index(text)
    msl = select_message(rows, "msl", "sfc", None)
    assert (msl["_offset"], msl["_length"]) == (10, 20)
    t850 = select_message(rows, "t", "pl", "850")
    assert t850["_offset"] == 30
    try:
        select_message(rows, "t", "pl", "700")
    except LookupError:
        pass
    else:
        raise AssertionError("a missing level must not select another temperature")


def test_crop_keeps_the_northwest_and_southeast_corners():
    # Global-like 0.25° grid, 90N to 90S, 0E to 359.75E, value = lat * 1000 + lon.
    lat0, lat1 = 90.0, -90.0
    lon0, lon1 = 0.0, 359.75
    ny, nx = 721, 1440
    lats = lat0 + np.arange(ny) * ((lat1 - lat0) / (ny - 1))
    lons = lon0 + np.arange(nx) * ((lon1 - lon0) / (nx - 1))
    grid = lats[:, None] * 1000.0 + lons[None, :]
    cropped = crop_north_up(grid, lat0, lon0, lat1, lon1)
    assert cropped.shape == (201, 301)
    assert cropped.dtype == np.float32
    assert abs(float(cropped[0, 0]) - (0.0 * 1000 + 95.0)) < 1e-3
    assert abs(float(cropped[0, -1]) - 170.0) < 1e-3
    assert abs(float(cropped[-1, 0]) - (-50.0 * 1000 + 95.0)) < 1e-3
    assert abs(float(cropped[-1, -1]) - (-50.0 * 1000 + 170.0)) < 1e-3
    # A node halfway down the west edge is 25S.
    assert abs(float(cropped[100, 0]) - (-25.0 * 1000 + 95.0)) < 1e-3


def test_crop_follows_a_grid_that_starts_at_the_date_line():
    lat0, dlat = 90.0, -0.25
    lon0, dlon = 180.0, 0.25
    ny, nx = 721, 1440
    lats = lat0 + np.arange(ny) * dlat
    lons = (lon0 + np.arange(nx) * dlon) % 360.0
    grid = lats[:, None] * 1000.0 + lons[None, :]
    cropped = crop_north_up(grid, lat0, lon0, float(lats[-1]), float(lons[-1]), dlat=dlat, dlon=dlon)
    assert cropped.shape == (201, 301)
    assert abs(float(cropped[0, 0]) - 95.0) < 1e-3
    assert abs(float(cropped[0, -1]) - 170.0) < 1e-3
    assert abs(float(cropped[-1, 0]) - (-50.0 * 1000 + 95.0)) < 1e-3
    assert abs(float(cropped[128, 83]) - (-32.0 * 1000 + 115.75)) < 1e-3


def test_units_become_hectopascals_celsius_and_millimetres():
    assert abs(float(to_display_units(np.array([101325.0]), "msl", "Pa")[0]) - 1013.25) < 1e-4
    assert abs(float(to_display_units(np.array([273.15]), "t", "K")[0])) < 1e-4
    assert abs(float(to_display_units(np.array([0.002]), "tp", "m")[0]) - 2.0) < 1e-4
    wind = to_display_units(np.array([3.0]), "10u", "m s**-1")
    assert abs(float(wind[0]) - 3.0) < 1e-6


def test_accumulation_window_is_a_full_day_not_the_running_total():
    start = np.array([1.0, 4.0], dtype=np.float32)
    end = np.array([2.4, 4.0], dtype=np.float32)
    window = accumulation_window(end, start)
    assert abs(float(window[0]) - 1.4) < 1e-5
    assert float(window[1]) == 0.0
    noise = accumulation_window(np.array([1.0]), np.array([1.02]))
    assert float(noise[0]) == 0.0
    reset = accumulation_window(np.array([0.2]), np.array([5.0]))
    assert float(reset[0]) < 0


def test_saturday_issue_and_the_early_frame_share_one_rain_definition():
    saturday = datetime(2026, 9, 26, 5, 56, tzinfo=UTC)
    panels = prognosis_times(saturday)
    assert len(panels) == 8
    assert panels[0] == datetime(2026, 9, 27, 0, tzinfo=UTC)
    assert panels[-1] == datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert all((panels[i] - panels[i - 1]) == timedelta(hours=12) for i in range(1, 8))
    times = chart_times(saturday)
    assert times[0] == datetime(2026, 9, 26, 6, tzinfo=UTC)
    latest = datetime(2026, 9, 26, 0, tzinfo=UTC)
    # Now is only 6 h into the run, so the 24 h window is the previous cycle.
    now_run, now_end, now_start = rain_source(latest, times[0])
    assert now_run == datetime(2026, 9, 25, 0, tzinfo=UTC)
    assert (now_end, now_start) == (30, 6)
    assert now_end - now_start == 24
    # +12 h is still inside the first day, so it uses that same previous cycle.
    # Sunday 00Z is 24 h into the latest run and subtracts step 0. Both windows
    # are 24 h of accumulated tp, which is what makes the hatches comparable.
    plus = rain_source(latest, times[0] + timedelta(hours=12))
    assert plus[0] == datetime(2026, 9, 25, 0, tzinfo=UTC)
    assert plus[1:] == (42, 18)
    sunday = rain_source(latest, panels[0])
    assert sunday == (latest, 24, 0)
    assert lead_hours(latest, panels[-1]) == 108
