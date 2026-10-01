"""Kite shore class, gust factor, sea breeze, and the rideable window."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from isobar_data.config import load_config
from isobar_data.derive import (
    build_kite,
    gust_factor,
    sea_breeze,
    shore_class,
    tide_extrema,
)
from isobar_data.kite_job import daylight_from_point, hours_from_point

UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures"


def _thresholds():
    kite = load_config().kite
    return {
        "speed_min_kt": kite.speed_min_kt,
        "speed_max_kt": kite.speed_max_kt,
        "excluded_shore": kite.excluded_shore,
        "daylight_required": kite.daylight_required,
        "gust_max_kt": kite.gust_max_kt,
        "sea_breeze_min": kite.sea_breeze_min,
        "sea_breeze_max": kite.sea_breeze_max,
        "sea_breeze_above_kt": kite.sea_breeze_above_kt,
        "timezone": kite.timezone,
        "airport_wmo": kite.airport_wmo,
    }


def test_cottesloe_offshore_hour_has_an_empty_window():
    product = json.loads((FIXTURES / "surface_cottesloe.json").read_text())
    hours = hours_from_point(product)
    product_doc = build_kite(
        spot_id="cottesloe",
        onshore_from_deg=270,
        osm_way=29283079,
        thresholds=_thresholds(),
        hours=hours,
        inland_hours=[],
        daylight=daylight_from_point(product),
        marine=None,
        observations=[],
        notice="Situational awareness only — not a flight briefing",
    )
    hour = product_doc["hours"][0]
    assert hour["shore_class"] == "offshore"
    assert abs(hour["gust_factor"] - 21.2 / 15.6) < 1e-9
    assert product_doc["windows"] == []
    assert "active" not in product_doc["sea_breeze"]


def test_an_onshore_hour_inside_the_band_is_one_window():
    moment = datetime(2026, 9, 26, 5, tzinfo=UTC)
    hours = [{
        "time": moment,
        "speed_kt": 18.0,
        "direction_deg": 270.0,
        "gust_kt": 22.0,
    }]
    daylight = [(moment - timedelta(hours=1), moment + timedelta(hours=1))]
    product = build_kite(
        spot_id="cottesloe",
        onshore_from_deg=270,
        osm_way=29283079,
        thresholds=_thresholds(),
        hours=hours,
        inland_hours=[],
        daylight=daylight,
        marine=None,
        observations=[],
        notice="",
    )
    assert len(product["windows"]) == 1
    assert product["windows"][0]["peak_kt"] == 18.0
    assert product["windows"][0]["classes"] == ["onshore"]


def test_shore_boundaries_and_gust_factor():
    assert shore_class(270, 270) == "onshore"
    assert shore_class(270 + 45, 270) == "onshore"
    assert shore_class(270 + 45.1, 270) == "cross-on"
    assert shore_class(270 + 90, 270) == "cross-on"
    assert shore_class(270 + 90.1, 270) == "cross-off"
    assert shore_class(270 + 135, 270) == "cross-off"
    assert shore_class(270 + 135.1, 270) == "offshore"
    assert abs(gust_factor(22, 12) - 22 / 12) < 1e-9
    assert gust_factor(10, 0.5) is None


def test_sea_breeze_onset_is_the_first_hour_after_one_that_was_not():
    start = datetime(2026, 9, 26, 0, tzinfo=UTC)
    coastal = []
    inland = {}
    for index, (direction, speed, inland_speed) in enumerate((
        (180, 8, 8),
        (220, 16, 8),
        (230, 18, 9),
    )):
        moment = start + timedelta(hours=index)
        coastal.append({
            "time": moment,
            "speed_kt": speed,
            "direction_deg": direction,
            "gust_kt": speed + 4,
        })
        inland[moment] = {"speed_kt": inland_speed}
    result = sea_breeze(
        coastal,
        inland,
        direction_min=200,
        direction_max=250,
        above_kt=5,
        timezone_name="Australia/Perth",
    )
    day = result["days"][0]
    assert day["onset"] == "2026-09-26T01:00:00Z"
    assert day["speed_kt"] == 16
    assert day["max_kt"] == 18
    assert result["active"] == ["2026-09-26T01:00:00Z", "2026-09-26T02:00:00Z"]


def test_tide_extrema_are_local_highs_and_lows():
    start = datetime(2026, 9, 26, 0, tzinfo=UTC)
    times = [start + timedelta(hours=index) for index in range(5)]
    tide = tide_extrema(times, [0, 1, 0, -1, 0])
    assert [row["time"] for row in tide["highs"]] == [
        "2026-09-26T01:00:00Z",
        "2026-09-26T04:00:00Z",
    ]
    assert [row["time"] for row in tide["lows"]] == [
        "2026-09-26T00:00:00Z",
        "2026-09-26T03:00:00Z",
    ]
    assert "not suitable for coastal navigation" in tide["caution"]


def test_kite_file_carries_open_meteo_and_shore_attribution(tmp_path):
    from isobar_data.kite_job import write_kite_files
    from isobar_data.ledger import build_manifest

    write_kite_files(tmp_path, load_config())
    files = list((tmp_path / "products" / "kite").rglob("cottesloe.json"))
    assert files
    product = json.loads(files[0].read_text())
    assert product["schema_version"] == 1
    assert product["contract"] == "isobar-data"
    assert product["family"] == "kite"
    assert "open-meteo-cc-by-4.0" in product.get("licence_ids", [])
    assert "openstreetmap-odbl" in product.get("licence_ids", [])
    assert any("open-meteo.com" in line for line in product.get("attribution", []))
    assert product.get("field_licences", {}).get("hours") == "open-meteo-cc-by-4.0"
    assert product.get("field_licences", {}).get("shore_source") == "openstreetmap-odbl"
    manifest = build_manifest(tmp_path, datetime(2026, 9, 26, tzinfo=UTC))
    entry = next(item for item in manifest["products"] if item["id"] == "kite-cottesloe")
    assert "open-meteo-cc-by-4.0" in entry.get("licence_ids", [])
    assert "openstreetmap-odbl" in entry.get("licence_ids", [])
