"""Each normaliser against a saved fixture."""

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from isobar_data.derive import summarise_members, wind_components
from isobar_data.normalise import (
    aviation_product,
    ceiling_feet,
    connect_obs,
    ends_for,
    ensemble_product,
    filter_runways,
    freezing_from_upper,
    insert_obs,
    is_warning_name,
    pack_f16,
    parse_metar,
    rows_from_obs_document,
    runway_components,
    sigmet_features,
    visibility_metres,
    warning_identifier,
    warning_title,
)

UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_obs_keeps_nulls_and_ignores_a_repeat_insert(tmp_path):
    document = _load("obs_station.json")
    rows, station = rows_from_obs_document(document, "IDW60910")
    assert rows[0]["vis_km"] is None
    assert rows[0]["cloud_base_m"] is None
    assert station["lat"] == -31.956
    assert station["lon"] == 115.7619
    connection = connect_obs(tmp_path / "obs.sqlite")
    assert insert_obs(connection, rows) == 1
    rows[0]["air_temp"] = 99
    assert insert_obs(connection, rows) == 0
    stored = connection.execute("SELECT air_temp, vis_km FROM obs").fetchone()
    assert stored["air_temp"] == 16.3
    assert stored["vis_km"] is None
    connection.close()


def test_metar_visibility_ceiling_and_crosswind(tmp_path):
    ypph = parse_metar(_load("metar_ypph.json"))
    yssy = parse_metar(_load("metar_yssy.json"))
    assert ypph["visibility_m"] == 9999
    assert visibility_metres(ypph["raw"]) == 9999
    assert ypph["ceiling_ft"] is None
    assert ypph["cloud_base_ft"] == 3300
    assert yssy["visibility_m"] == 10000
    assert yssy["ceiling_ft"] is None
    assert ceiling_feet([{"cover": "SCT", "base": 3300}, {"cover": "BKN", "base": 2500}], ypph["raw"]) == 2500

    runways = filter_runways((FIXTURES / "runways.csv").read_text(), {"YPPH", "YSSY"})
    idents = {row["icao"] for row in runways["runways"]}
    assert idents == {"YPPH", "YSSY"}
    assert any(row["closed"] for row in runways["runways"])
    ypph_ends = ends_for(runways, "YPPH")
    yssy_ends = ends_for(runways, "YSSY")
    ypph_rows = {row["end"]: row for row in runway_components(ypph, ypph_ends)}
    yssy_rows = {row["end"]: row for row in runway_components(yssy, yssy_ends)}
    assert ypph_rows["21"]["headwind_kt"] == 7.6
    assert ypph_rows["21"]["crosswind_kt"] == -10.5
    assert ypph_rows["06"]["headwind_kt"] == 2.0
    assert ypph_rows["06"]["crosswind_kt"] == 12.8
    assert yssy_rows["07"]["headwind_kt"] == 10.8
    assert yssy_rows["07"]["crosswind_kt"] == -10.4
    assert yssy_rows["34R"]["headwind_kt"] == 11.1
    assert yssy_rows["34R"]["crosswind_kt"] == 10.0

    moment = datetime(2026, 9, 26, 5, 30, tzinfo=UTC)
    upper = _load("upper_ypph.json")
    level, note = freezing_from_upper(upper, moment)
    assert level == 2362
    assert note == ""
    below, below_note = freezing_from_upper(
        {"time": ["2026-09-26T05:00:00Z"], "levels": {"500": {"temperature_c": [-10], "height_m": [5000]}}},
        moment,
    )
    assert below is None
    assert below_note == "freezing level is below the lowest level"
    above, above_note = freezing_from_upper(
        {"time": ["2026-09-26T05:00:00Z"], "levels": {"1000": {"temperature_c": [12], "height_m": [100]}}},
        moment,
    )
    assert above is None
    assert above_note == "freezing level is above the highest level"

    product = aviation_product(
        icao="YPPH",
        metar=_load("metar_ypph.json"),
        taf=_load("taf_ypph.json"),
        ends=ypph_ends,
        history=[],
        freezing=(level, note),
        model=None,
    )
    assert "fltCat" not in json.dumps(product)
    assert product["taf"]["db_pop_time"] == "2026-09-26T05:37:37.339Z"
    mixed = aviation_product(
        icao="YPPH",
        metar=_load("metar_ypph.json"),
        taf=_load("taf_ypph.json"),
        ends=ypph_ends,
        history=[],
        freezing=(level, note),
        model={"label": "ecmwf_ifs 9 km", "time": ["2026-09-26T05:00:00Z"], "visibility_m": [10000]},
    )
    assert "open-meteo-cc-by-4.0" in mixed["licence_ids"]
    assert "aviationweather" in mixed["licence_ids"]
    assert mixed["field_licences"]["model"] == "open-meteo-cc-by-4.0"
    assert mixed["field_licences"]["freezing_level_m"] == "open-meteo-cc-by-4.0"
    assert any("open-meteo.com" in line for line in mixed["attribution"])
    from isobar_data.ledger import build_manifest
    from isobar_data.normalise import dump_json

    dump_json(tmp_path / "products" / "aviation" / "YPPH.json", mixed)
    entry = next(item for item in build_manifest(tmp_path, moment)["products"] if item["id"] == "aviation-YPPH")
    assert "open-meteo-cc-by-4.0" in entry["licence_ids"]
    assert "aviationweather" in entry["licence_ids"]
    head, cross = wind_components(140, 13, 194)
    assert round(head, 1) == 7.6
    assert round(cross, 1) == -10.5


def test_sigmet_keeps_the_two_australian_firs():
    kept = sigmet_features(_load("sigmet.json"))
    assert [row["firId"] for row in kept] == ["YMMM", "YBBB"]
    wrapped = sigmet_features({"features": [{"properties": item} for item in _load("sigmet.json")]})
    assert [row["firId"] for row in wrapped] == ["YMMM", "YBBB"]


def test_point_products_identify_their_family_contract():
    from isobar_data.openmeteo import normalise_point

    body = {
        "latitude": -32.0,
        "longitude": 115.7,
        "hourly": {"time": ["2026-09-26T00:00"], "temperature_2m": [18]},
        "hourly_units": {"temperature_2m": "°C"},
    }
    for kind, family in (
        ("surface", "points/ecmwf_ifs"),
        ("marine", "points/marine"),
        ("upper", "points/ecmwf_ifs025_upper"),
    ):
        product = normalise_point(
            body, kind=kind, point_id="test", run="2026-09-26T00:00:00Z",
            native_step=1, model="fixture",
        )
        assert product["schema_version"] == 1
        assert product["contract"] == "isobar-data"
        assert product["family"] == family


def test_ensemble_percentiles_and_warning_title():
    summary = summarise_members([[1], [2], [3], [4], [5]])
    assert summary["p10"] == [1.4]
    assert summary["p90"] == [4.6]
    product = ensemble_product(_load("ensemble.json"), point_id="swanbourne", run="2026-09-25T18:00:00Z")
    assert product["schema_version"] == 1
    assert product["contract"] == "isobar-data"
    assert product["family"] == "ensemble"
    assert product["variables"]["pressure_msl"]["min"][0] == 1000
    assert product["variables"]["pressure_msl"]["max"][0] == 1008
    xml = (FIXTURES / "warning.xml").read_text()
    assert warning_identifier(xml) == "IDW20100"
    assert warning_title(xml) == "Marine Wind Warning Summary for Western Australia"
    assert is_warning_name("IDW20100.xml")
    assert is_warning_name("IDN20400.xml")
    assert not is_warning_name("IDW60910.xml")


def test_pack_f16_maps_nan_to_the_fill_sentinel():
    packed = pack_f16([float("nan")])
    assert packed == np.array([-32768], dtype="<f2").tobytes()
    assert packed == b"\x00\xf8"
