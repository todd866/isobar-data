"""Join the 9 km series, the marine series, and the obs table into the kite files."""

from __future__ import annotations

from datetime import datetime, timezone

from isobar_data.config import Config
from isobar_data.contract import marker
from isobar_data.derive import build_kite
from isobar_data.identity import NOTICE
from isobar_data.ledger import attribution_for
from isobar_data.normalise import connect_obs, encode_json, latest_obs
from isobar_data.publish import commit_files, read_published


def _parse(text: str) -> datetime:
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def hours_from_point(product: dict) -> list[dict]:
    hourly = product.get("hourly") or {}
    speeds = hourly.get("wind_speed_10m") or []
    directions = hourly.get("wind_direction_10m") or []
    gusts = hourly.get("wind_gusts_10m") or []
    hours = []
    for index, text in enumerate(product.get("time") or []):
        hours.append({
            "time": _parse(text),
            "speed_kt": _at(speeds, index),
            "direction_deg": _at(directions, index),
            "gust_kt": _at(gusts, index),
        })
    return hours


def daylight_from_point(product: dict) -> list[tuple[datetime, datetime]]:
    daily = product.get("daily") or {}
    sunrises = daily.get("sunrise") or []
    sunsets = daily.get("sunset") or []
    intervals = []
    for rise, settle in zip(sunrises, sunsets):
        if rise and settle:
            intervals.append((_parse(rise), _parse(settle)))
    return intervals


def marine_from_point(product: dict | None) -> dict | None:
    if not product:
        return None
    hourly = product.get("hourly") or {}
    times = [_parse(text) for text in product.get("time") or []]
    return {
        "snapped": {
            "latitude": product.get("latitude"),
            "longitude": product.get("longitude"),
        },
        "times": times,
        "sea_level_height_msl": hourly.get("sea_level_height_msl") or [],
    }


def _at(values, index):
    if index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    return float(value)


def write_kite_files(root, config: Config) -> dict:
    thresholds = {
        "speed_min_kt": config.kite.speed_min_kt,
        "speed_max_kt": config.kite.speed_max_kt,
        "excluded_shore": config.kite.excluded_shore,
        "daylight_required": config.kite.daylight_required,
        "gust_max_kt": config.kite.gust_max_kt,
        "sea_breeze_min": config.kite.sea_breeze_min,
        "sea_breeze_max": config.kite.sea_breeze_max,
        "sea_breeze_above_kt": config.kite.sea_breeze_above_kt,
        "timezone": config.kite.timezone,
        "airport_wmo": config.kite.airport_wmo,
    }
    inland = read_published(root, "points/ecmwf_ifs", f"{config.kite.inland_point}.json")
    inland_hours = hours_from_point(inland) if inland else []
    obs_path = root / "products" / "obs" / "obs.sqlite"
    observations = []
    if obs_path.is_file():
        connection = connect_obs(obs_path)
        try:
            observations = latest_obs(connection, list(config.coastal_wmo))
        finally:
            connection.close()
    written = []
    staged = []
    for spot in config.spots:
        surface = read_published(root, "points/ecmwf_ifs", f"{spot.id}.json")
        marine = read_published(root, "points/marine", f"{spot.id}.json") if spot.marine else None
        hours = hours_from_point(surface) if surface else []
        product = build_kite(
            spot_id=spot.id,
            onshore_from_deg=float(spot.onshore_from_deg),
            osm_way=spot.osm_way,
            thresholds=thresholds,
            hours=hours,
            inland_hours=inland_hours,
            daylight=daylight_from_point(surface) if surface else [],
            marine=marine_from_point(marine),
            observations=observations,
            notice=NOTICE,
        )
        licence_ids = ["open-meteo-cc-by-4.0", "openstreetmap-odbl"]
        if observations:
            licence_ids.append("bom-anonymous-ftp")
        product["licence_id"] = licence_ids[0]
        product["licence_ids"] = licence_ids
        product["attribution"] = attribution_for(licence_ids)
        product["field_licences"] = {
            "hours": "open-meteo-cc-by-4.0",
            "windows": "open-meteo-cc-by-4.0",
            "sea_breeze": "open-meteo-cc-by-4.0",
            "tide": "open-meteo-cc-by-4.0",
            "shore_source": "openstreetmap-odbl",
            "observations": "bom-anonymous-ftp",
        }
        product["run"] = None if not surface else surface.get("run")
        product.update(marker(family="kite"))
        staged.append((spot.id, product))
        written.append(spot.id)
    if not staged:
        return {"ok": True, "complete": True, "detail": "", "run": None}
    # The inland model time stays put while observations and marine fields move.
    # A content token publishes a new directory instead of editing that run.
    commit_files(
        root,
        "kite",
        {f"{spot_id}.json": encode_json(product) for spot_id, product in staged},
        keep="latest",
    )
    return {"ok": True, "complete": True, "detail": ", ".join(written), "run": None if not inland else inland.get("run")}
