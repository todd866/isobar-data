"""Open-Meteo point products. The national grid is ECMWF Open Data, not this host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from isobar_data.contract import marker
from isobar_data.derive import open_meteo_calls
from isobar_data.http import Http, Later
from isobar_data.normalise import dump_json, ensemble_product, subsample_hours
from isobar_data.publish import publish_run, run_dir
from isobar_data.storage import store_raw

FORECAST = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
MARINE = "https://marine-api.open-meteo.com/v1/marine"

META = {
    "ecmwf_ifs": "https://api.open-meteo.com/data/ecmwf_ifs/static/meta.json",
    "ecmwf_ifs025": "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json",
    "ecmwf_ifs025_ensemble": "https://ensemble-api.open-meteo.com/data/ecmwf_ifs025_ensemble/static/meta.json",
}
# Best Match has no single meta document. Gate on the two models that cover Perth.
MARINE_METAS = (
    "https://marine-api.open-meteo.com/data/ecmwf_wam/static/meta.json",
    "https://marine-api.open-meteo.com/data/ncep_gfswave025/static/meta.json",
)

SURFACE_HOURLY = (
    "temperature_2m",
    "dew_point_2m",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "precipitation",
    "cape",
    "visibility",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "weather_code",
)

# Keep the pressure ladder dense enough to show the troposphere and lower
# stratosphere. Open-Meteo exposes these IFS 0.25° pressure-level variables
# directly, including the derived geometric vertical velocity.
UPPER_LEVELS_HPA = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50)
# Daily names the ecmwf_ifs forecast aggregates in the location's timezone.
# All of these are valid on that model.
SURFACE_DAILY = (
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
SURFACE_HOURS = 168


def upper_hourly() -> tuple[str, ...]:
    names = []
    for level in UPPER_LEVELS_HPA:
        names.extend([
            f"temperature_{level}hPa",
            f"wind_speed_{level}hPa",
            f"wind_direction_{level}hPa",
            f"geopotential_height_{level}hPa",
            f"relative_humidity_{level}hPa",
            f"cloud_cover_{level}hPa",
            f"vertical_velocity_{level}hPa",
        ])
    return tuple(names)


def _hourly_profile(hourly: tuple[str, ...], daily: tuple[str, ...]) -> str:
    return ",".join(hourly + daily)


MARINE_HOURLY = (
    "wave_height",
    "wave_direction",
    "wave_period",
    "swell_wave_height",
    "swell_wave_direction",
    "swell_wave_period",
    "wind_wave_height",
    "wind_wave_direction",
    "wind_wave_period",
    "secondary_swell_wave_height",
    "secondary_swell_wave_direction",
    "secondary_swell_wave_period",
    "sea_surface_temperature",
    "sea_level_height_msl",
)


@dataclass(frozen=True)
class MetaDecision:
    fetch_body: bool
    init: datetime | None
    available: datetime | None
    not_before: datetime | None
    detail: str


def as_utc(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value).replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def meta_decision(meta: dict, now: datetime, stored_init: str | None) -> MetaDecision:
    """Wait 10 minutes after availability. A repeated init is not fetched again.

    The previous-runs API is not used. The forecast endpoint serves the current run.
    """
    init = as_utc(meta["last_run_initialisation_time"])
    available = as_utc(meta["last_run_availability_time"])
    init_iso = init.strftime("%Y-%m-%dT%H:%M:%SZ")
    if now < available + timedelta(minutes=10):
        return MetaDecision(
            False,
            init,
            available,
            available + timedelta(minutes=10),
            "within 10 min of availability",
        )
    if stored_init == init_iso:
        return MetaDecision(False, init, available, None, "run already stored")
    return MetaDecision(True, init, available, None, "")


def forecast_params(
    points: list[tuple[str, float, float]],
    *,
    hourly: tuple[str, ...],
    daily: tuple[str, ...] = (),
    model: str | None,
    hours: int | None,
    days: int | None,
    cell: str,
    wind_kn: bool,
    elevation_nan: bool,
    tz: str = "GMT",
) -> str:
    pairs: list[tuple[str, str]] = [
        ("latitude", ",".join(str(lat) for _ident, lat, _lon in points)),
        ("longitude", ",".join(str(lon) for _ident, _lat, lon in points)),
        ("hourly", ",".join(hourly)),
        ("cell_selection", cell),
        ("timezone", tz),
    ]
    if daily:
        pairs.append(("daily", ",".join(daily)))
    if model:
        pairs.append(("models", model))
    if hours is not None:
        pairs.append(("forecast_hours", str(hours)))
    if days is not None:
        pairs.append(("forecast_days", str(days)))
    if wind_kn:
        pairs.append(("wind_speed_unit", "kn"))
    if elevation_nan:
        for _point in points:
            pairs.append(("elevation", "nan"))
    return urlencode(pairs, safe=",")


def _subsample_body(body: dict, step_hours: int) -> dict:
    hourly = dict(body.get("hourly") or {})
    times = list(hourly.get("time") or [])
    indexes = subsample_hours(times, step_hours)
    kept = {"time": [times[index] for index in indexes]}
    for key, values in hourly.items():
        if key == "time":
            continue
        kept[key] = _slice(values, indexes)
    cloned = dict(body)
    cloned["hourly"] = kept
    return cloned


def _bodies(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    raise RuntimeError("Open-Meteo response was not JSON")


def _slice(values, indexes: list[int]):
    if not isinstance(values, list):
        return values
    return [values[index] for index in indexes if index < len(values)]


def _local_zone(body: dict):
    """Clock offset of a non-GMT Open-Meteo body. GMT responses stay untouched.

    Open-Meteo writes every local time in the series at one fixed
    ``utc_offset_seconds`` (the offset at request time), even across a
    daylight-saving change, so the IANA zone must not be used to read them.
    """
    name = body.get("timezone")
    if not name or str(name) in {"GMT", "UTC", "Etc/UTC", "Etc/GMT"}:
        return None
    offset = body.get("utc_offset_seconds")
    if not isinstance(offset, (int, float)) or isinstance(offset, bool):
        raise ValueError(f"Open-Meteo body for {name} has no utc_offset_seconds")
    return timezone(timedelta(seconds=int(offset)))


def _gmt_hour(text: str, zone) -> str:
    moment = datetime.fromisoformat(str(text))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _local_clock(text, zone):
    if not text:
        return text
    moment = datetime.fromisoformat(str(text))
    if moment.tzinfo is not None:
        return str(text)
    return moment.replace(tzinfo=zone).isoformat(timespec="minutes")


def normalise_point(body: dict, *, kind: str, point_id: str, run: str, native_step: int, model: str) -> dict:
    hourly = body.get("hourly") or {}
    times = list(hourly.get("time") or [])
    indexes = subsample_hours(times, native_step)
    zone = _local_zone(body)
    kept_times = [
        _gmt_hour(times[index], zone) if zone is not None else times[index]
        for index in indexes
    ]
    kept_hourly = {}
    units = body.get("hourly_units") or {}
    for key, values in hourly.items():
        if key == "time":
            continue
        kept_hourly[key] = _slice(values, indexes)
    daily = dict(body.get("daily") or {})
    if zone is not None:
        for key in ("sunrise", "sunset"):
            values = daily.get(key)
            if isinstance(values, list):
                daily[key] = [_local_clock(value, zone) for value in values]
    product = {
        **marker(family={
            "surface": "points/ecmwf_ifs",
            "marine": "points/marine",
            "upper": "points/ecmwf_ifs025_upper",
        }.get(kind, "points")),
        "id": point_id,
        "licence_id": "open-meteo-cc-by-4.0",
        "model": model,
        "run": run,
        "native_step_hours": native_step,
        "latitude": body.get("latitude"),
        "longitude": body.get("longitude"),
        "elevation": body.get("elevation"),
        "units": units,
        "time": kept_times,
        "hourly": kept_hourly,
        "daily": daily,
        "valid_time": kept_times[-1] if kept_times else None,
    }
    if zone is not None:
        # Daily dates and sunrise/sunset are local. Hourly `time` stays GMT.
        product["timezone"] = str(body["timezone"])
    if kind == "marine":
        from isobar_data.derive import MARINE_CAUTION

        product["snapped"] = {"latitude": body.get("latitude"), "longitude": body.get("longitude")}
        product["caution"] = MARINE_CAUTION
    if kind == "upper":
        product["levels"] = _upper_levels(kept_hourly)
    return product


def _upper_levels(hourly: dict) -> dict:
    levels = {}
    for level in UPPER_LEVELS_HPA:
        entry = {}
        temp = hourly.get(f"temperature_{level}hPa")
        height = hourly.get(f"geopotential_height_{level}hPa")
        speed = hourly.get(f"wind_speed_{level}hPa")
        direction = hourly.get(f"wind_direction_{level}hPa")
        humidity = hourly.get(f"relative_humidity_{level}hPa")
        cover = hourly.get(f"cloud_cover_{level}hPa")
        vertical_velocity = hourly.get(f"vertical_velocity_{level}hPa")
        if temp is not None:
            entry["temperature_c"] = temp
        if height is not None:
            entry["height_m"] = height
        if speed is not None:
            entry["wind_speed_kt"] = speed
        if direction is not None:
            entry["wind_direction_deg"] = direction
        if humidity is not None:
            entry["relative_humidity_pct"] = humidity
        if cover is not None:
            entry["cloud_cover_pct"] = cover
        if vertical_velocity is not None:
            # Open-Meteo returns the API's geometric vertical velocity in m/s.
            # Keep the unit in the field name so consumers cannot confuse it
            # with ECMWF's native pressure velocity (omega, Pa/s).
            entry["vertical_velocity_ms"] = vertical_velocity
        if entry:
            levels[str(level)] = entry
    return levels


def fetch_job(
    http: Http,
    root: Path,
    state: dict,
    now: datetime,
    *,
    job_id: str,
    endpoint: str,
    meta_urls: tuple[str, ...],
    points: list[tuple[str, float, float]],
    hourly: tuple[str, ...],
    daily: tuple[str, ...] = (),
    model: str | None,
    hours: int | None,
    days: int | None,
    cell: str,
    wind_kn: bool,
    elevation_nan: bool,
    native_step: int,
    kind: str,
    product_dir: Path,
    tz: str = "GMT",
) -> dict:
    if not points:
        return {"ok": True, "complete": True, "detail": "no points configured", "run": None, "calls": 0}
    metas = []
    for url in meta_urls:
        stored_etag = (state.get("validators") or {}).get(url, {}).get("etag")
        response = http.get(url, bucket="open-meteo", weight=1, etag=stored_etag, timeout=30)
        if response.status_code == 304 and not (state.get("metas") or {}).get(url):
            response = http.get(url, bucket="open-meteo", weight=1, timeout=30)
        if response.status_code == 304:
            cached = (state.get("metas") or {}).get(url)
            if not cached:
                raise RuntimeError(f"meta {url} was not modified and is not stored")
            metas.append(cached)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"meta {url} HTTP {response.status_code}: {response.text[:200]}")
        meta = response.json()
        state.setdefault("metas", {})[url] = meta
        state.setdefault("validators", {})[url] = {"etag": response.headers.get("etag")}
        metas.append(meta)
    stored = (state.get("sources") or {}).get(job_id, {}).get("watermark")
    profile = _hourly_profile(hourly, daily)
    stored_profile = (state.get("sources") or {}).get(job_id, {}).get("variables")
    profile_current = stored_profile == profile
    stored_init = stored if len(metas) == 1 and profile_current else None
    decisions = [
        meta_decision(meta, now, stored_init) for meta in metas
    ]
    if any(item.not_before for item in decisions):
        when = max(item.not_before for item in decisions if item.not_before)
        raise Later(when, "within 10 min of availability")
    inits = [item.init.strftime("%Y-%m-%dT%H:%M:%SZ") for item in decisions if item.init]
    watermark = "|".join(inits)
    if (stored == watermark and profile_current) or (len(metas) == 1 and not decisions[0].fetch_body):
        return {
            "ok": True,
            "complete": True,
            "detail": "run already stored",
            "run": inits[0] if inits else None,
            "available": decisions[0].available.strftime("%Y-%m-%dT%H:%M:%SZ") if decisions[0].available else None,
            "calls": len(meta_urls),
        }
    variables = len(hourly) + len(daily)
    models = 1
    day_count = days if days is not None else (hours or 24) / 24
    weight = open_meteo_calls(variables, models, day_count, len(points))
    query = forecast_params(
        points,
        hourly=hourly,
        daily=daily,
        model=model,
        hours=hours,
        days=days,
        cell=cell,
        wind_kn=wind_kn,
        elevation_nan=elevation_nan,
        tz=tz,
    )
    response = http.get(f"{endpoint}?{query}", bucket="open-meteo", weight=weight, timeout=120, accept="application/json")
    if response.status_code != 200:
        raise RuntimeError(f"{job_id} HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    raw_path = root / "raw" / "open-meteo" / job_id / watermark.replace(":", "") / "body.json"
    store_raw(raw_path, response.content, compressible=True)
    bodies = _bodies(payload)
    if len(bodies) != len(points):
        raise RuntimeError(f"{job_id} returned {len(bodies)} locations, expected {len(points)}")
    run = max(inits) if inits else None
    family = product_dir.resolve().relative_to((root / "products").resolve()).as_posix()
    run_token = (run or watermark).replace(":", "")
    stage = run_dir(root, family, run_token)
    required = []
    dated = []
    for (point_id, _lat, _lon), body in zip(points, bodies):
        if native_step > 1:
            body = _subsample_body(body, native_step)
        if kind == "ensemble":
            product = ensemble_product(body, point_id=point_id, run=run)
            product["native_step_hours"] = native_step
            # Keep the member matrix in raw only. The product is the percentiles.
            dated.append((point_id, product))
        else:
            product = normalise_point(
                body,
                kind=kind,
                point_id=point_id,
                run=run,
                native_step=native_step,
                model=model or "best-match",
            )
            if kind == "upper":
                product["levels"] = product.get("levels") or _upper_levels(product["hourly"])
        dump_json(stage / f"{point_id}.json", product)
        required.append(f"{point_id}.json")
    publish_run(root, family, run_token, required, keep="latest")
    for point_id, product in dated:
        dump_json(product_dir / point_id / f"{run_token}.json", product)
    source_state = state.setdefault("sources", {}).setdefault(job_id, {})
    source_state["watermark"] = watermark
    source_state["variables"] = profile
    return {
        "ok": True,
        "complete": True,
        "detail": "",
        "run": run,
        "available": decisions[0].available.strftime("%Y-%m-%dT%H:%M:%SZ") if decisions[0].available else None,
        "calls": weight + len(meta_urls),
    }
