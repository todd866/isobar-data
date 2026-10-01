"""Token buckets sit 5% under the published caps."""

from isobar_data.derive import chunk_ranges, open_meteo_calls
from isobar_data.scheduler import CHUNK_GAP
from isobar_data.tokens import aviation_bucket, open_meteo_bucket
from datetime import timedelta


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


def test_chunks_of_250_leave_a_minute_between_them():
    parts = chunk_ranges(300, 250)
    assert [list(part)[0] for part in parts] == [0, 250]
    assert list(parts[0])[-1] == 249
    assert list(parts[1]) == list(range(250, 300))
    assert CHUNK_GAP == timedelta(minutes=1)
