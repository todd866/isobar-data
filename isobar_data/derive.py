"""Kite, crosswind, freezing level, tide, and Open-Meteo call weight.

Pure functions. No network.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

MARINE_CAUTION = (
    "Tides and ocean currents are computed at 0.08° (~8 km) resolution using "
    "numerical models. Accuracy at coastal areas is limited. This is not suitable "
    "for coastal navigation and does not replace your nautical almanac. Use with caution!"
)

SHORE = ("onshore", "cross-on", "cross-off", "offshore")

COMPASS = {
    "N": 0.0,
    "NNE": 22.5,
    "NE": 45.0,
    "ENE": 67.5,
    "E": 90.0,
    "ESE": 112.5,
    "SE": 135.0,
    "SSE": 157.5,
    "S": 180.0,
    "SSW": 202.5,
    "SW": 225.0,
    "WSW": 247.5,
    "W": 270.0,
    "WNW": 292.5,
    "NW": 315.0,
    "NNW": 337.5,
}
SECTOR_HALF_DEG = 11.25


def open_meteo_calls(variables: int, models: int, days: float, locations: int) -> float:
    """Pricing-page weight. Ensemble members are not a factor."""
    if locations < 1:
        return 0.0
    weight = max(1.0, (variables * models / 10.0) * max(1.0, days / 14.0))
    return weight * locations


def chunk_ranges(count: int, size: int = 250) -> list[range]:
    if count < 0 or size < 1:
        raise ValueError("chunk size must be positive")
    return [range(start, min(start + size, count)) for start in range(0, count, size)]


def wrap_signed(degrees: float) -> float:
    """Wrap a difference into [-180, 180]."""
    return (degrees + 180.0) % 360.0 - 180.0


def shore_class(direction_deg: float, onshore_from_deg: float) -> str:
    magnitude = abs(wrap_signed(direction_deg - onshore_from_deg))
    if magnitude <= 45:
        return "onshore"
    if magnitude <= 90:
        return "cross-on"
    if magnitude <= 135:
        return "cross-off"
    return "offshore"


def gust_factor(gust_kt: float | None, speed_kt: float | None) -> float | None:
    if speed_kt is None or gust_kt is None or speed_kt < 1:
        return None
    return float(gust_kt) / float(speed_kt)


def in_sector(direction: float, start: float, end: float) -> bool:
    direction = direction % 360
    start = start % 360
    end = end % 360
    if start <= end:
        return start <= direction <= end
    return direction >= start or direction <= end


def compass_centre(name: str | None) -> float | None:
    if not name:
        return None
    return COMPASS.get(name.strip().upper())


def wind_components(direction_deg: float, speed_kt: float, heading_deg: float) -> tuple[float, float]:
    """Headwind and crosswind. Crosswind is positive from the pilot's right."""
    delta = wrap_signed(direction_deg - heading_deg)
    radians = math.radians(delta)
    crosswind = speed_kt * math.sin(radians)
    headwind = speed_kt * math.cos(radians)
    return headwind, crosswind


def freezing_level_m(levels: list[tuple[float | None, float | None]]) -> tuple[int | None, str]:
    """Interpolate 0 °C between bracketing levels. Never extrapolate."""
    points = [(float(t), float(h)) for t, h in levels if t is not None and h is not None]
    points.sort(key=lambda item: item[1])
    if not points:
        return None, "no temperature profile"
    if all(temp < 0 for temp, _height in points):
        return None, "freezing level is below the lowest level"
    if all(temp > 0 for temp, _height in points):
        return None, "freezing level is above the highest level"
    for temp, height in points:
        if temp == 0:
            return int(round(height)), ""
    for (temp0, height0), (temp1, height1) in zip(points, points[1:]):
        if temp0 > 0 >= temp1 or temp0 >= 0 > temp1:
            if temp0 == temp1:
                continue
            fraction = temp0 / (temp0 - temp1)
            height = height0 + fraction * (height1 - height0)
            return int(round(height)), ""
    if points[0][0] < 0:
        return None, "freezing level is below the lowest level"
    return None, "freezing level is above the highest level"


def tide_extrema(
    times: list[datetime], heights: list[float | None]
) -> dict[str, list[dict]]:
    """Hourly local maxima and minima. Endpoints use their one neighbour."""
    highs: list[dict] = []
    lows: list[dict] = []
    for index, height in enumerate(heights):
        if height is None:
            continue
        left = heights[index - 1] if index else None
        right = heights[index + 1] if index + 1 < len(heights) else None
        neighbours = [value for value in (left, right) if value is not None]
        if not neighbours:
            continue
        is_high = all(height >= value for value in neighbours) and any(height > value for value in neighbours)
        is_low = all(height <= value for value in neighbours) and any(height < value for value in neighbours)
        stamp = times[index]
        row = {"time": _iso(stamp), "height_m": height}
        if is_high:
            highs.append(row)
        if is_low:
            lows.append(row)
    return {"highs": highs, "lows": lows, "caution": MARINE_CAUTION}


def member_keys(hourly: dict, variable: str) -> list[str]:
    prefix = variable + "_member"
    members = [key for key in hourly if key.startswith(prefix) and key[len(prefix):].isdigit()]
    keys = []
    if variable in hourly:
        keys.append(variable)
    keys.extend(sorted(members))
    return keys


def summarise_members(series: list[list[float | None]]) -> dict[str, list[float | None]]:
    """Mean, min, max, and the 10th and 90th percentiles at each hour."""
    if not series:
        return {"mean": [], "min": [], "max": [], "p10": [], "p90": []}
    length = max(len(row) for row in series)
    out: dict[str, list[float | None]] = {key: [] for key in ("mean", "min", "max", "p10", "p90")}
    for index in range(length):
        values = []
        for row in series:
            if index < len(row) and row[index] is not None:
                values.append(float(row[index]))
        if not values:
            for key in out:
                out[key].append(None)
            continue
        array = np.asarray(values, dtype=np.float64)
        out["mean"].append(float(array.mean()))
        out["min"].append(float(array.min()))
        out["max"].append(float(array.max()))
        out["p10"].append(float(np.percentile(array, 10)))
        out["p90"].append(float(np.percentile(array, 90)))
    return out


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _qualifies(hour: dict, thresholds: dict, daylight: list[tuple[datetime, datetime]]) -> bool:
    speed = hour.get("speed_kt")
    direction = hour.get("direction_deg")
    if speed is None or direction is None:
        return False
    if speed < thresholds["speed_min_kt"] or speed > thresholds["speed_max_kt"]:
        return False
    shore = shore_class(direction, thresholds["onshore_from_deg"])
    if shore in thresholds["excluded_shore"]:
        return False
    gust_max = thresholds.get("gust_max_kt")
    if gust_max is not None:
        gust = hour.get("gust_kt")
        if gust is None or gust > gust_max:
            return False
    if thresholds.get("daylight_required", True):
        moment = hour["time"]
        if not any(start <= moment <= end for start, end in daylight):
            return False
    return True


def rideable_windows(hours: list[dict], thresholds: dict, daylight: list[tuple[datetime, datetime]]) -> list[dict]:
    """Maximal runs of hours inside the configured band."""
    windows: list[dict] = []
    current: list[dict] = []

    def close() -> None:
        if not current:
            return
        classes: list[str] = []
        for hour in current:
            shore = shore_class(hour["direction_deg"], thresholds["onshore_from_deg"])
            if shore not in classes:
                classes.append(shore)
        peak = max(hour["speed_kt"] for hour in current)
        windows.append({
            "start": _iso(current[0]["time"]),
            "end": _iso(current[-1]["time"]),
            "peak_kt": peak,
            "classes": classes,
        })

    for hour in hours:
        if _qualifies(hour, thresholds, daylight):
            current.append(hour)
        elif current:
            close()
            current = []
    close()
    return windows


def sea_breeze(
    coastal: list[dict],
    inland_by_time: dict[datetime, dict],
    *,
    direction_min: float,
    direction_max: float,
    above_kt: float,
    timezone_name: str,
) -> dict:
    """First transition into the house sea-breeze test, per local day.

    Observations do not create an onset. This uses the 9 km hours only.
    """
    zone = ZoneInfo(timezone_name)
    flags = []
    for hour in coastal:
        speed = hour.get("speed_kt")
        direction = hour.get("direction_deg")
        inland = inland_by_time.get(hour["time"])
        inland_speed = None if inland is None else inland.get("speed_kt")
        ok = (
            speed is not None
            and direction is not None
            and inland_speed is not None
            and in_sector(direction, direction_min, direction_max)
            and speed >= inland_speed + above_kt
        )
        flags.append(ok)

    by_day: OrderedDict[str, dict] = OrderedDict()
    active: list[str] = []
    seen_previous = False
    previous = False
    index = 0
    while index < len(coastal):
        hour = coastal[index]
        local_date = hour["time"].astimezone(zone).date().isoformat()
        day = by_day.get(local_date)
        if day is None:
            day = {"date": local_date, "onset": None, "speed_kt": None, "gust_kt": None, "max_kt": None}
            by_day[local_date] = day
        flag = flags[index]
        if flag and seen_previous and not previous and day["onset"] is None:
            run_max = hour["speed_kt"]
            cursor = index
            while cursor < len(coastal) and flags[cursor]:
                run_max = max(run_max, coastal[cursor]["speed_kt"])
                active.append(_iso(coastal[cursor]["time"]))
                cursor += 1
            day["onset"] = _iso(hour["time"])
            day["speed_kt"] = hour["speed_kt"]
            day["gust_kt"] = hour.get("gust_kt")
            day["max_kt"] = run_max
        seen_previous = True
        previous = flag
        index += 1
    return {"label": "sea-breeze", "days": list(by_day.values()), "active": active}


def observation_check(rows: list[dict], *, direction_min: float, direction_max: float, above_kt: float, airport_wmo: int) -> dict:
    """Sector-centre comparison. It does not set an onset time."""
    by_wmo = {}
    for row in rows:
        by_wmo[int(row["wmo"])] = row
    airport = by_wmo.get(airport_wmo)
    airport_speed = None if airport is None else airport.get("wind_spd_kt")
    checks = []
    for wmo, row in by_wmo.items():
        centre = compass_centre(row.get("wind_dir"))
        checks.append({
            "wmo": wmo,
            "name": row.get("name"),
            "time": row.get("aifstime_utc"),
            "wind_dir": row.get("wind_dir"),
            "sector_centre_deg": centre,
            "sector_half_deg": SECTOR_HALF_DEG,
            "wind_spd_kt": row.get("wind_spd_kt"),
            "gust_kt": row.get("gust_kt"),
            "gust_factor": gust_factor(row.get("gust_kt"), row.get("wind_spd_kt")),
        })
    return {
        "onset": None,
        "note": "Observation check only. It does not create a sea-breeze onset.",
        "airport_wmo": airport_wmo,
        "airport_wind_spd_kt": airport_speed,
        "direction_min_deg": direction_min,
        "direction_max_deg": direction_max,
        "stations": checks,
    }


def build_kite(
    *,
    spot_id: str,
    onshore_from_deg: float,
    osm_way: int | None,
    thresholds: dict,
    hours: list[dict],
    inland_hours: list[dict],
    daylight: list[tuple[datetime, datetime]],
    marine: dict | None,
    observations: list[dict],
    notice: str,
) -> dict:
    """One spot file. An empty window is a normal result."""
    gate = {
        "speed_min_kt": thresholds["speed_min_kt"],
        "speed_max_kt": thresholds["speed_max_kt"],
        "excluded_shore": list(thresholds["excluded_shore"]),
        "daylight_required": thresholds.get("daylight_required", True),
        "gust_max_kt": thresholds.get("gust_max_kt"),
        "onshore_from_deg": onshore_from_deg,
    }
    enriched = []
    for hour in hours:
        direction = hour.get("direction_deg")
        speed = hour.get("speed_kt")
        gust = hour.get("gust_kt")
        shore = None if direction is None else shore_class(direction, onshore_from_deg)
        enriched.append({
            "time": _iso(hour["time"]),
            "speed_kt": speed,
            "direction_deg": direction,
            "gust_kt": gust,
            "gust_factor": gust_factor(gust, speed),
            "shore_class": shore,
        })
    inland_by_time = {hour["time"]: hour for hour in inland_hours}
    breeze = sea_breeze(
        hours,
        inland_by_time,
        direction_min=thresholds["sea_breeze_min"],
        direction_max=thresholds["sea_breeze_max"],
        above_kt=thresholds["sea_breeze_above_kt"],
        timezone_name=thresholds["timezone"],
    )
    active = set(breeze.pop("active", []))
    for row, hour in zip(enriched, hours):
        row["sea_breeze"] = _iso(hour["time"]) in active
    tide = {"highs": [], "lows": [], "caution": MARINE_CAUTION}
    snapped = None
    if marine:
        snapped = marine.get("snapped")
        times = marine.get("times") or []
        heights = marine.get("sea_level_height_msl") or []
        if times and heights:
            tide = tide_extrema(times, heights)
    return {
        "id": spot_id,
        "notice": notice,
        "onshore_from_deg": onshore_from_deg,
        "shore_source": {
            "osm_way": osm_way,
            "licence_id": "openstreetmap-odbl",
        },
        "thresholds": {
            "speed_min_kt": gate["speed_min_kt"],
            "speed_max_kt": gate["speed_max_kt"],
            "excluded_shore": gate["excluded_shore"],
            "daylight_required": gate["daylight_required"],
            "gust_max_kt": gate["gust_max_kt"],
        },
        "hours": enriched,
        "windows": rideable_windows(hours, gate, daylight),
        "sea_breeze": breeze,
        "tide": tide,
        "marine_snapped": snapped,
        "marine_caution": MARINE_CAUTION,
        "observations": observations,
        "observation_check": observation_check(
            observations,
            direction_min=thresholds["sea_breeze_min"],
            direction_max=thresholds["sea_breeze_max"],
            above_kt=thresholds["sea_breeze_above_kt"],
            airport_wmo=thresholds["airport_wmo"],
        ),
    }

