"""Turn fetched documents into the products Isobar reads."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from isobar_data.contract import marker
from isobar_data.derive import (
    freezing_level_m,
    member_keys,
    summarise_members,
    wind_components,
)
from isobar_data.identity import NOTICE

FILL = -32768
VISIBILITY_RE = re.compile(
    r"\b(?:\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT\s+(CAVOK|\d{4})\b"
)
WARNING_RE = re.compile(r"^(IDW|IDN)2\d{4}\.xml$")


def number(value):
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rows_from_obs_document(document: dict, default_product: str) -> tuple[list[dict], dict | None]:
    """Bureau observation JSON. ``vis_km`` of ``-`` stays null. Cloud base stays null."""
    observations = document.get("observations") or {}
    header = observations.get("header") or []
    header_row = header[0] if isinstance(header, list) and header else {}
    product = header_row.get("ID") or default_product
    station = None
    if header_row.get("wmo_id"):
        station = {
            "wmo": int(header_row["wmo_id"]),
            "name": header_row.get("name"),
        }
    rows = []
    for item in observations.get("data") or []:
        if item.get("wmo") is None or not item.get("aifstime_utc"):
            continue
        row = {
            "wmo": int(item["wmo"]),
            "aifstime_utc": str(item["aifstime_utc"]),
            "product_id": item.get("history_product") or product,
            "name": item.get("name"),
            "lat": number(item.get("lat")),
            "lon": number(item.get("lon")),
            "air_temp": number(item.get("air_temp")),
            "wind_dir": item.get("wind_dir"),
            "wind_dir_deg": number(item.get("wind_dir_deg")),
            "wind_spd_kmh": number(item.get("wind_spd_kmh")),
            "gust_kmh": number(item.get("gust_kmh")),
            "wind_spd_kt": number(item.get("wind_spd_kt")),
            "gust_kt": number(item.get("gust_kt")),
            "press_msl": number(item.get("press_msl")),
            "press_tend": None if item.get("press_tend") is None else str(item.get("press_tend")),
            "rain_trace": None if item.get("rain_trace") is None else str(item.get("rain_trace")),
            "cloud_base_m": number(item.get("cloud_base_m")),
            "vis_km": number(item.get("vis_km")),
        }
        rows.append(row)
        if station is None or station.get("lat") is None:
            station = {"wmo": row["wmo"], "name": row["name"], "lat": row["lat"], "lon": row["lon"]}
    return rows, station


OBS_COLUMNS = (
    "wmo",
    "aifstime_utc",
    "product_id",
    "name",
    "lat",
    "lon",
    "air_temp",
    "wind_dir",
    "wind_dir_deg",
    "wind_spd_kmh",
    "gust_kmh",
    "wind_spd_kt",
    "gust_kt",
    "press_msl",
    "press_tend",
    "rain_trace",
    "cloud_base_m",
    "vis_km",
)


def connect_obs(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS obs (
            wmo INTEGER NOT NULL,
            aifstime_utc TEXT NOT NULL,
            product_id TEXT NOT NULL,
            name TEXT,
            lat REAL,
            lon REAL,
            air_temp REAL,
            wind_dir TEXT,
            wind_dir_deg REAL,
            wind_spd_kmh REAL,
            gust_kmh REAL,
            wind_spd_kt REAL,
            gust_kt REAL,
            press_msl REAL,
            press_tend TEXT,
            rain_trace TEXT,
            cloud_base_m REAL,
            vis_km REAL,
            PRIMARY KEY (wmo, aifstime_utc)
        )
        """
    )
    return connection


def insert_obs(connection: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in OBS_COLUMNS)
    names = ", ".join(OBS_COLUMNS)
    before = connection.total_changes
    connection.executemany(
        f"INSERT OR IGNORE INTO obs ({names}) VALUES ({placeholders})",
        [tuple(row.get(column) for column in OBS_COLUMNS) for row in rows],
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return connection.total_changes - before


def latest_obs(connection: sqlite3.Connection, wmos: list[int]) -> list[dict]:
    found = []
    for wmo in wmos:
        row = connection.execute(
            "SELECT * FROM obs WHERE wmo = ? ORDER BY aifstime_utc DESC LIMIT 1",
            (int(wmo),),
        ).fetchone()
        if row is not None:
            found.append({key: row[key] for key in row.keys()})
    return found


def station_coord(connection: sqlite3.Connection, wmo: int) -> tuple[float, float] | None:
    row = connection.execute(
        "SELECT lat, lon FROM obs WHERE wmo = ? AND lat IS NOT NULL ORDER BY aifstime_utc DESC LIMIT 1",
        (int(wmo),),
    ).fetchone()
    if row is None:
        return None
    return float(row["lat"]), float(row["lon"])


def visibility_metres(raw: str) -> int | None:
    """Australian visibility is the raw group, in metres. Ignore a US ``6+``."""
    if not raw:
        return None
    match = VISIBILITY_RE.search(raw)
    if not match:
        if "CAVOK" in raw:
            return 10000
        return None
    token = match.group(1)
    if token == "CAVOK":
        return 10000
    return int(token)


def ceiling_feet(clouds: list[dict] | None, raw: str) -> int | None:
    if raw and "CAVOK" in raw:
        return None
    bases = []
    for layer in clouds or []:
        cover = (layer.get("cover") or "").upper()
        if cover in {"BKN", "OVC", "VV"} and layer.get("base") is not None:
            bases.append(int(layer["base"]))
    if not bases:
        return None
    return min(bases)


def lowest_base_feet(clouds: list[dict] | None, raw: str) -> int | None:
    if raw and "CAVOK" in raw:
        return None
    bases = [int(layer["base"]) for layer in clouds or [] if layer.get("base") is not None]
    if not bases:
        return None
    return min(bases)


def parse_metar(record: dict) -> dict:
    raw = record.get("rawOb") or ""
    clouds = record.get("clouds") or []
    direction = record.get("wdir")
    if isinstance(direction, str):
        direction = None if direction.upper() == "VRB" else number(direction)
    return {
        "icao": record.get("icaoId"),
        "raw": raw,
        "time": record.get("reportTime") or _unix_iso(record.get("obsTime")),
        "wind_dir_true": None if direction is None else int(direction),
        "wind_speed_kt": number(record.get("wspd")),
        "gust_kt": number(record.get("wgst")),
        "visibility_m": visibility_metres(raw),
        "ceiling_ft": ceiling_feet(clouds, raw),
        "cloud_base_ft": lowest_base_feet(clouds, raw),
        "clouds": [
            {"cover": layer.get("cover"), "base_ft": layer.get("base")}
            for layer in clouds
        ],
    }


def parse_taf(record: dict | None) -> dict | None:
    if not record:
        return None
    return {
        "raw": record.get("rawTAF") or "",
        "db_pop_time": record.get("dbPopTime"),
        "issue_time": record.get("issueTime"),
        "valid_from": record.get("validTimeFrom"),
        "valid_to": record.get("validTimeTo"),
    }


def runway_components(parsed: dict, ends: list[dict]) -> list[dict]:
    rows = []
    direction = parsed.get("wind_dir_true")
    speed = parsed.get("wind_speed_kt")
    gust = parsed.get("gust_kt")
    for end in ends:
        heading = end.get("heading_true")
        head = cross = gust_head = gust_cross = None
        if direction is not None and speed is not None and heading is not None and not end.get("closed"):
            head, cross = wind_components(float(direction), float(speed), float(heading))
            if gust is not None:
                gust_head, gust_cross = wind_components(float(direction), float(gust), float(heading))
        rows.append({
            "end": end.get("ident"),
            "heading_true": heading,
            "length_ft": end.get("length_ft"),
            "displaced_threshold_ft": end.get("displaced_threshold_ft"),
            "closed": bool(end.get("closed")),
            "headwind_kt": None if head is None else round(head, 1),
            "crosswind_kt": None if cross is None else round(cross, 1),
            "gust_headwind_kt": None if gust_head is None else round(gust_head, 1),
            "gust_crosswind_kt": None if gust_cross is None else round(gust_cross, 1),
        })
    return rows


def aviation_product(
    *,
    icao: str,
    metar: dict | None,
    taf: dict | None,
    ends: list[dict],
    history: list[dict],
    freezing: tuple[int | None, str] | None,
    model: dict | None,
) -> dict:
    from isobar_data.ledger import attribution_for

    parsed = parse_metar(metar) if metar else None
    field_licences = {}
    licence_ids = []
    if metar or taf or history:
        licence_ids.append("aviationweather")
    if metar:
        field_licences["metar"] = "aviationweather"
    if taf:
        field_licences["taf"] = "aviationweather"
    if history:
        field_licences["trend"] = "aviationweather"
    if ends:
        licence_ids.append("ourairports-public-domain")
        field_licences["runways"] = "ourairports-public-domain"
    if freezing is not None or model:
        licence_ids.append("open-meteo-cc-by-4.0")
    if freezing is not None:
        field_licences["freezing_level_m"] = "open-meteo-cc-by-4.0"
    if model:
        field_licences["model"] = "open-meteo-cc-by-4.0"
        model = dict(model)
        model["licence_id"] = "open-meteo-cc-by-4.0"
    if not licence_ids:
        licence_ids.append("aviationweather")
    product = {
        "notice": NOTICE,
        "icao": icao,
        "metar": parsed,
        "taf": parse_taf(taf),
        "runways": runway_components(parsed, ends) if parsed else [],
        "trend": history,
        "freezing_level_m": None if freezing is None else freezing[0],
        "freezing_level_note": "" if freezing is None else freezing[1],
        "model": model,
        "licence_id": licence_ids[0],
        "licence_ids": licence_ids,
        "field_licences": field_licences,
        "attribution": attribution_for(licence_ids),
    }
    return product


def profile_at(upper: dict, moment: datetime | None) -> list[tuple[float | None, float | None]]:
    """Temperature and height pairs for the hour nearest ``moment``."""
    times = upper.get("time") or []
    levels = upper.get("levels") or {}
    if not times:
        return []
    index = 0
    if moment is not None:
        stamps = [_parse_time(item) for item in times]
        index = min(range(len(stamps)), key=lambda i: abs((stamps[i] - moment).total_seconds()))
    pairs = []
    for name, level in levels.items():
        temps = level.get("temperature_c") or []
        heights = level.get("height_m") or []
        temp = temps[index] if index < len(temps) else None
        height = heights[index] if index < len(heights) else None
        pairs.append((temp, height))
    return pairs


def freezing_from_upper(upper: dict, moment: datetime | None) -> tuple[int | None, str]:
    return freezing_level_m(profile_at(upper, moment))


def sigmet_features(document) -> list[dict]:
    """Keep YMMM and YBBB from either a feature collection or a flat list."""
    if isinstance(document, dict):
        items = document.get("features") or []
    elif isinstance(document, list):
        items = document
    else:
        items = []
    kept = []
    for item in items:
        props = item.get("properties") if isinstance(item, dict) and "properties" in item else item
        if not isinstance(props, dict):
            continue
        fir = props.get("firId")
        if fir not in {"YMMM", "YBBB"}:
            continue
        kept.append({
            "firId": fir,
            "firName": props.get("firName"),
            "hazard": props.get("hazard"),
            "qualifier": props.get("qualifier"),
            "base": props.get("base"),
            "top": props.get("top"),
            "raw": props.get("rawSigmet"),
            "valid_from": props.get("validTimeFrom"),
            "valid_to": props.get("validTimeTo"),
        })
    return kept


def filter_runways(csv_text: str, icaos: set[str]) -> dict:
    wanted = {icao.upper() for icao in icaos}
    rows = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for record in reader:
        ident = (record.get("airport_ident") or "").upper()
        if ident not in wanted:
            continue
        rows.append({
            "icao": ident,
            "length_ft": number(record.get("length_ft")),
            "closed": str(record.get("closed") or "0").strip() not in {"0", ""},
            "le_ident": record.get("le_ident") or "",
            "le_heading_degT": number(record.get("le_heading_degT")),
            "le_displaced_threshold_ft": number(record.get("le_displaced_threshold_ft")),
            "he_ident": record.get("he_ident") or "",
            "he_heading_degT": number(record.get("he_heading_degT")),
            "he_displaced_threshold_ft": number(record.get("he_displaced_threshold_ft")),
        })
    return {"licence_id": "ourairports-public-domain", "runways": rows}


def ends_for(runways_doc: dict, icao: str) -> list[dict]:
    ends = []
    for row in runways_doc.get("runways") or []:
        if row.get("icao") != icao:
            continue
        length = row.get("length_ft")
        closed = row.get("closed")
        ends.append({
            "ident": row.get("le_ident"),
            "heading_true": row.get("le_heading_degT"),
            "length_ft": length,
            "displaced_threshold_ft": row.get("le_displaced_threshold_ft"),
            "closed": closed,
        })
        ends.append({
            "ident": row.get("he_ident"),
            "heading_true": row.get("he_heading_degT"),
            "length_ft": length,
            "displaced_threshold_ft": row.get("he_displaced_threshold_ft"),
            "closed": closed,
        })
    return ends


def ensemble_product(body: dict, *, point_id: str, run: str) -> dict:
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    variables = {}
    for name in ("pressure_msl", "temperature_850hPa"):
        keys = member_keys(hourly, name)
        series = [hourly.get(key) or [] for key in keys]
        variables[name] = summarise_members(series)
    return {
        **marker(family="ensemble"),
        "id": point_id,
        "licence_id": "open-meteo-cc-by-4.0",
        "model": "ecmwf_ifs025_ensemble",
        "run": run,
        "latitude": body.get("latitude"),
        "longitude": body.get("longitude"),
        "time": times,
        "variables": variables,
    }


def warning_title(xml: str) -> str:
    match = re.search(r'type="warning_title">(.*?)</text>', xml, re.S)
    if not match:
        return ""
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    return " ".join(text.split())


def warning_identifier(xml: str) -> str:
    match = re.search(r"<identifier>([^<]+)</identifier>", xml)
    return match.group(1).strip() if match else ""


def is_warning_name(name: str) -> bool:
    return WARNING_RE.match(name) is not None


def pack_f16(grid) -> bytes:
    """Little-endian float16. Non-finite values become the fill sentinel."""
    array = np.asarray(grid, dtype=np.float64)
    output = np.empty(array.shape, dtype="<f2")
    finite = np.isfinite(array)
    clipped = np.clip(array, -65504, 65504)
    output[finite] = clipped[finite].astype(np.float16)
    output[~finite] = np.float16(FILL)
    return np.ascontiguousarray(output).tobytes()


def subsample_hours(times: list[str], step_hours: int) -> list[int]:
    if step_hours <= 1:
        return list(range(len(times)))
    kept = []
    for index, text in enumerate(times):
        moment = _parse_time(text)
        if moment.minute == 0 and moment.second == 0 and moment.hour % step_hours == 0:
            kept.append(index)
    return kept


def _parse_time(text: str) -> datetime:
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _unix_iso(value) -> str | None:
    if value is None:
        return None
    moment = datetime.fromtimestamp(int(value), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def json_ready(value):
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, (np.floating,)):
        number_ = float(value)
        return None if not np.isfinite(number_) else number_
    if isinstance(value, (np.integer,)):
        return int(value)
    return value


def encode_json(obj: dict) -> bytes:
    return json.dumps(json_ready(obj), indent=2, ensure_ascii=False).encode() + b"\n"


def dump_json(path: Path, obj: dict) -> None:
    from isobar_data.storage import write_if_changed

    write_if_changed(path, encode_json(obj))
