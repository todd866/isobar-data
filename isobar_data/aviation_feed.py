"""METAR, TAF, SIGMET and PIREP from aviationweather.gov."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from isobar_data.contract import marker
from isobar_data.http import Http
from isobar_data.normalise import (
    aviation_product,
    dump_json,
    encode_json,
    ends_for,
    freezing_from_upper,
    parse_metar,
    sigmet_features,
)
from isobar_data.publish import commit_files, published_path, read_pointer, read_published, run_dir
from isobar_data.storage import store_raw

METAR = "https://aviationweather.gov/api/data/metar"
TAF = "https://aviationweather.gov/api/data/taf"
SIGMET = "https://aviationweather.gov/api/data/isigmet"
PIREP = "https://aviationweather.gov/api/data/pirep"


def commit_validator(state: dict, key: str, etag: str | None, modified: str | None) -> None:
    """Record a validator only after the body has been stored."""
    state.setdefault("validators", {})[key] = {"etag": etag, "modified": modified}


def _conditional(http: Http, url: str, state: dict, key: str) -> tuple[int, bytes | None, str | None, str | None]:
    validators = (state.get("validators") or {}).get(key) or {}
    response = http.get(
        url,
        bucket="aviationweather",
        weight=1,
        etag=validators.get("etag"),
        modified=validators.get("modified"),
        accept="application/json",
        timeout=40,
    )
    etag = response.headers.get("etag")
    modified = response.headers.get("last-modified")
    if response.status_code == 304:
        return 304, None, validators.get("etag"), validators.get("modified")
    if response.status_code == 204:
        return 204, b"", etag, modified
    if response.status_code != 200:
        raise RuntimeError(f"{url} HTTP {response.status_code}: {response.text[:200]}")
    return 200, response.content, etag, modified


def _history(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS metar (
            icao TEXT NOT NULL,
            obs_time TEXT NOT NULL,
            raw TEXT,
            visibility_m REAL,
            cloud_base_ft REAL,
            ceiling_ft REAL,
            PRIMARY KEY (icao, obs_time)
        )
        """
    )
    return connection


def fetch_metar_taf(http: Http, root: Path, state: dict, icaos: list[str], *, which: str) -> dict:
    ident = ",".join(icaos)
    base = METAR if which == "metar" else TAF
    url = f"{base}?ids={ident}&format=json"
    status, body, etag, modified = _conditional(http, url, state, which)
    if status == 304:
        return {"ok": True, "complete": True, "detail": "not modified", "written": False}
    payload = json.loads(body.decode()) if body else []
    if isinstance(payload, dict):
        payload = [payload]
    raw_path = root / "raw" / "aviation" / which / f"{which}.json"
    store_raw(raw_path, body or b"[]", compressible=True)
    by_icao = {item.get("icaoId"): item for item in payload}
    if which == "metar":
        connection = _history(root / "products" / "aviation" / "history.sqlite")
        try:
            for icao, record in by_icao.items():
                if not icao:
                    continue
                parsed = parse_metar(record)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO metar (icao, obs_time, raw, visibility_m, cloud_base_ft, ceiling_ft)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        icao,
                        parsed.get("time") or "",
                        parsed.get("raw"),
                        parsed.get("visibility_m"),
                        parsed.get("cloud_base_ft"),
                        parsed.get("ceiling_ft"),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
    state.setdefault("aviation", {})[which] = by_icao
    commit_validator(state, which, etag, modified)
    newest = None
    for record in payload:
        stamp = record.get("reportTime") or record.get("issueTime") or record.get("dbPopTime")
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    return {"ok": True, "complete": True, "detail": "", "run": newest, "written": True, "count": len(payload)}


def fetch_sigmet(http: Http, root: Path, state: dict) -> dict:
    url = f"{SIGMET}?format=json"
    status, body, etag, modified = _conditional(http, url, state, "sigmet")
    if status == 304:
        return {"ok": True, "complete": True, "detail": "not modified", "run": state.get("sources", {}).get("sigmet", {}).get("run")}
    document = json.loads(body.decode()) if body else []
    store_raw(root / "raw" / "aviation" / "sigmet" / "isigmet.json", body or b"[]", compressible=True)
    kept = sigmet_features(document)
    publish_aviation(root, {
        "sigmet.json": {
            "notice": "Situational awareness only — not a flight briefing",
            "features": kept,
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "Aviation Weather Center",
            "licence_id": "aviationweather",
            "licence_ids": ["aviationweather"],
        },
    })
    commit_validator(state, "sigmet", etag, modified)
    return {"ok": True, "complete": True, "detail": f"{len(kept)} in YMMM/YBBB", "run": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "count": len(kept)}


def fetch_pirep(http: Http, root: Path, state: dict, icao: str = "YPPH") -> dict:
    url = f"{PIREP}?id={icao}&distance=300&format=json"
    status, body, etag, modified = _conditional(http, url, state, "pirep")
    if status == 304:
        return {"ok": True, "complete": True, "detail": "not modified", "run": state.get("sources", {}).get("pirep", {}).get("run")}
    reports = []
    if status == 200 and body:
        payload = json.loads(body.decode())
        reports = payload if isinstance(payload, list) else []
        store_raw(root / "raw" / "aviation" / "pirep" / "pirep.json", body, compressible=True)
    publish_aviation(root, {
        "pirep.json": {
            "notice": "Situational awareness only — not a flight briefing",
            "reports": reports,
            "licence_id": "aviationweather",
            "licence_ids": ["aviationweather"],
        },
    })
    commit_validator(state, "pirep", etag, modified)
    detail = "empty" if not reports else f"{len(reports)} reports"
    if status == 204:
        detail = "empty"
    return {"ok": True, "complete": True, "detail": detail, "run": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def publish_aviation(root: Path, updates: dict[str, dict]) -> None:
    """Replace the aviation snapshot in a new run. The published run stays put."""
    current = _aviation_snapshot(root)
    current.update(updates)
    files = {
        name: encode_json({**product, **marker(family="aviation")})
        for name, product in current.items()
    }

    def write(stage: Path) -> None:
        for name, product in current.items():
            dump_json(stage / name, {**product, **marker(family="aviation")})

    commit_files(root, "aviation", files, keep="latest", write=write)


def _aviation_snapshot(root: Path) -> dict[str, dict]:
    latest = read_pointer(root, "aviation").get("latest")
    if latest:
        directory = run_dir(root, "aviation", latest)
    else:
        directory = root / "products" / "aviation"
    found = {}
    if not directory.is_dir():
        return found
    for path in directory.glob("*.json"):
        if path.name == "current.json":
            continue
        try:
            body = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict):
            found[path.name] = body
    return found


def write_aerodrome_files(root: Path, icaos: list[str], surface_ids: dict[str, str]) -> None:
    """Join the latest METAR, TAF, runways, upper air and 9 km model fields."""
    state_path = root / "state.json"
    # Callers pass the live state. This function reads products already on disk.
    metars = _load_cached(root, "metar")
    tafs = _load_cached(root, "taf")
    runways = {}
    runway_path = published_path(root, "aviation", "runways.json")
    if runway_path is not None:
        runways = json.loads(runway_path.read_text())
    history = _history(root / "products" / "aviation" / "history.sqlite")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    products: dict[str, dict] = {}
    try:
        for icao in icaos:
            metar = metars.get(icao)
            taf = tafs.get(icao)
            ends = ends_for(runways, icao) if runways else []
            rows = history.execute(
                "SELECT obs_time, visibility_m, cloud_base_ft FROM metar WHERE icao = ? AND obs_time >= ? ORDER BY obs_time",
                (icao, cutoff),
            ).fetchall()
            trend = [
                {"time": row[0], "visibility_m": row[1], "cloud_base_ft": row[2]}
                for row in rows
            ]
            freezing = None
            moment = None
            if metar and metar.get("reportTime"):
                text = str(metar["reportTime"]).replace("Z", "+00:00")
                try:
                    moment = datetime.fromisoformat(text)
                except ValueError:
                    moment = None
            upper = read_published(root, "points/ecmwf_ifs025_upper", f"{icao}.json")
            if upper:
                freezing = freezing_from_upper(upper, moment)
            model = None
            surface_id = surface_ids.get(icao)
            if surface_id:
                surface = read_published(root, "points/ecmwf_ifs", f"{surface_id}.json")
                if surface:
                    hourly = surface.get("hourly") or {}
                    model = {
                        "label": "ecmwf_ifs 9 km",
                        "time": surface.get("time"),
                        "visibility_m": hourly.get("visibility"),
                        "cloud_cover": hourly.get("cloud_cover"),
                        "licence_id": "open-meteo-cc-by-4.0",
                    }
            product = aviation_product(
                icao=icao,
                metar=metar,
                taf=taf,
                ends=ends,
                history=trend,
                freezing=freezing,
                model=model,
            )
            products[f"{icao}.json"] = product
    finally:
        history.close()
    if products:
        publish_aviation(root, products)
    del state_path


def _load_cached(root: Path, which: str) -> dict:
    # The scheduler keeps the latest decode in memory and also the raw file.
    path = root / "raw" / "aviation" / which / f"{which}.json"
    zst = path.with_name(path.name + ".zst")
    target = zst if zst.is_file() else path
    if not target.is_file():
        return {}
    from isobar_data.storage import read_maybe_zstd

    try:
        payload = json.loads(read_maybe_zstd(target).decode())
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(payload, dict):
        payload = [payload]
    return {item.get("icaoId"): item for item in payload if isinstance(item, dict)}
