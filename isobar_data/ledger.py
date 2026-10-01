"""status.json, manifest.json, and the append-only attribution ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from isobar_data.identity import NOTICE
from isobar_data.contract import marker
from isobar_data.storage import sha256, write_if_changed

LEDGER = (
    {
        "licence_id": "open-meteo-cc-by-4.0",
        "policy_url": "https://open-meteo.com/en/terms",
        "attribution": "Weather data by Open-Meteo.com (https://open-meteo.com/)",
        "redistribute": True,
    },
    {
        "licence_id": "ecmwf-cc-by-4.0",
        "policy_url": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
        "attribution": "ECMWF Open Data, CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)",
        "redistribute": True,
    },
    {
        "licence_id": "bom-anonymous-ftp",
        "policy_url": "https://www.bom.gov.au/copyright",
        "attribution": "© Bureau of Meteorology",
        "redistribute": False,
    },
    {
        "licence_id": "aviationweather",
        "policy_url": "https://aviationweather.gov/data/api/",
        "attribution": (
            "Aviation Weather Center (aviationweather.gov). The API page states no licence; "
            "the nearest statement found is the NWS disclaimer (https://www.weather.gov/disclaimer)."
        ),
        "redistribute": False,
    },
    {
        "licence_id": "ourairports-public-domain",
        "policy_url": "https://ourairports.com/data/",
        "attribution": "OurAirports runway data, public domain. Credit is optional.",
        "redistribute": True,
    },
    {
        "licence_id": "openstreetmap-odbl",
        "policy_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "Shore bearings from OpenStreetMap. Data © OpenStreetMap contributors, ODbL.",
        "redistribute": True,
    },
    {
        "licence_id": "notac-terms",
        "policy_url": "https://notac.aero/terms/",
        "attribution": "NOTAC · unofficial (https://notac.aero/)",
        # Provider attribution does not grant rights in originating States' text.
        "redistribute": False,
    },
)


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    """ISO-8601, a joined marine watermark, or an HTTP date."""
    if "|" in text:
        return max(parse_iso(part) for part in text.split("|") if part)
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        from email.utils import parsedate_to_datetime

        moment = parsedate_to_datetime(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def age_phrase(now: datetime, then: datetime) -> str:
    seconds = max(0, (now - then).total_seconds())
    if seconds < 3600:
        minutes = max(1, int(round(seconds / 60)))
        return f"{minutes} min ago"
    hours = int(round(seconds / 3600))
    if hours < 48:
        return f"{max(1, hours)} h ago"
    days = max(1, int(round(seconds / 86400)))
    return f"{days} d ago"


def source_label(name: str, run: datetime | None, now: datetime, *, cycle: bool) -> str:
    if run is None:
        return f"{name} · no data"
    if cycle:
        return f"{name} {run.strftime('%H')}Z · {age_phrase(now, run)}"
    return f"{name} · {age_phrase(now, run)}"


def merge_status(previous: dict | None, sources: list[dict], now: datetime) -> dict:
    return {"updated": iso(now), "sources": sources}


def failed_source(previous: dict | None, *, source_id: str, name: str, now: datetime, detail: str, cycle: bool) -> dict:
    run = None if not previous else previous.get("run")
    available = None if not previous else previous.get("available")
    moment = parse_iso(run) if run else None
    return {
        "id": source_id,
        "ok": False,
        "run": run,
        "available": available,
        "label": source_label(name, moment, now, cycle=cycle),
        "detail": detail,
    }


def healthy_source(
    *,
    source_id: str,
    name: str,
    now: datetime,
    run: str | None,
    available: str | None,
    detail: str,
    cycle: bool,
) -> dict:
    moment = parse_iso(run) if run else None
    return {
        "id": source_id,
        "ok": True,
        "run": run,
        "available": available,
        "label": source_label(name, moment, now, cycle=cycle),
        "detail": detail,
    }


def write_status(root: Path, status: dict) -> None:
    _write_json(root / "status.json", status)


def attribution_for(licence_ids: list[str]) -> list[str]:
    text = []
    for licence_id in licence_ids:
        for row in LEDGER:
            if row["licence_id"] == licence_id:
                text.append(row["attribution"])
                break
    return text


def write_attribution(root: Path) -> None:
    path = root / "attribution.json"
    existing: list[dict] = []
    if path.is_file():
        existing = json.loads(path.read_text()).get("sources") or []
    by_id = {row["licence_id"]: row for row in existing}
    for row in LEDGER:
        by_id.setdefault(row["licence_id"], dict(row))
    # Preserve any licence row that arrived earlier, even if this version no longer lists it.
    ordered = []
    seen = set()
    for row in existing:
        ordered.append(by_id[row["licence_id"]])
        seen.add(row["licence_id"])
    for row in LEDGER:
        if row["licence_id"] not in seen:
            ordered.append(by_id[row["licence_id"]])
    _write_json(path, {"sources": ordered})


def build_manifest(root: Path, now: datetime) -> dict:
    products = []
    products.extend(_grids(root))
    for family, licence, top_only in (
        ("points/ecmwf_ifs", "open-meteo-cc-by-4.0", False),
        ("points/ecmwf_ifs025_upper", "open-meteo-cc-by-4.0", False),
        ("points/marine", "open-meteo-cc-by-4.0", False),
        ("ensemble", "open-meteo-cc-by-4.0", True),
        ("kite", "open-meteo-cc-by-4.0", False),
    ):
        products.extend(_family_json(root, family, licence, top_only=top_only))
    products.extend(_family_json(root, "aviation", "aviationweather", top_only=True))
    products.extend(_published_binary(
        root, "charts", {".pdf", ".gif"}, "bom-anonymous-ftp", "chart", previous_prefix="chart-previous",
    ))
    products.extend(_published_binary(
        root, "warnings", {".xml"}, "bom-anonymous-ftp", "warning",
    ))
    obs = root / "products" / "obs" / "obs.sqlite"
    if obs.is_file():
        products.append(_obs_entry(root, obs))
    products.sort(key=lambda item: item["id"])
    return {**marker(), "notice": NOTICE, "updated": iso(now), "products": products}


def write_manifest(root: Path, manifest: dict) -> None:
    _write_json(root / "manifest.json", manifest)


def _write_json(path: Path, obj: dict) -> None:
    data = json.dumps(obj, indent=2, ensure_ascii=False).encode() + b"\n"
    write_if_changed(path, data)


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _licence_fields(body: dict | None, default: str) -> dict:
    ids: list[str] = []
    if isinstance(body, dict):
        raw = body.get("licence_ids")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item and item not in ids:
                    ids.append(item)
        if not ids and isinstance(body.get("licence_id"), str) and body.get("licence_id"):
            ids = [body["licence_id"]]
    if not ids:
        ids = [default]
    return {"licence_id": ids[0], "licence_ids": ids}


def _family_json(root: Path, family: str, licence: str, *, top_only: bool) -> list[dict]:
    from isobar_data.publish import read_pointer, run_dir

    pointer = read_pointer(root, family)
    if pointer["runs"]:
        entries = []
        for run_id in pointer["runs"]:
            entries.extend(_json_tree(
                root,
                run_dir(root, family, run_id),
                licence,
                top_only=top_only,
                family=family,
            ))
        return entries
    return _json_tree(root, root / "products" / family, licence, top_only=True, family=family)


def _grids(root: Path) -> list[dict]:
    from isobar_data.publish import read_pointer, run_dir

    entries = []
    family = "grids/ecmwf_ifs025"
    for run_id in read_pointer(root, family)["runs"]:
        entries.extend(_grid_files(root, run_dir(root, family, run_id)))
    entries.extend(_grid_files(root, root / "products" / "grids" / "ecmwf_ifs025" / "thin"))
    return entries


def _grid_files(root: Path, directory: Path) -> list[dict]:
    if not directory.exists():
        return []
    entries = []
    for path in directory.rglob("*.f16"):
        sidecar_path = path.with_suffix(".json")
        sidecar = json.loads(sidecar_path.read_text()) if sidecar_path.is_file() else {}
        entries.append({
            "id": "grid-" + path.relative_to(directory).as_posix().replace("/", "-").replace(".f16", ""),
            "path": _rel(root, path),
            "sidecar": _rel(root, sidecar_path) if sidecar_path.is_file() else None,
            "valid_time": sidecar.get("valid_time"),
            "run": sidecar.get("run"),
            "units": sidecar.get("units"),
            "grid": {
                "lat0": sidecar.get("lat0"),
                "lon0": sidecar.get("lon0"),
                "dlat": sidecar.get("dlat"),
                "dlon": sidecar.get("dlon"),
                "ny": sidecar.get("ny"),
                "nx": sidecar.get("nx"),
            },
            "stations": None,
            "sha256": sha256(path.read_bytes()),
            "licence_id": "ecmwf-cc-by-4.0",
            "licence_ids": ["ecmwf-cc-by-4.0"],
        })
    return entries


def _json_tree(
    root: Path,
    directory: Path,
    licence: str,
    *,
    top_only: bool = False,
    family: str | None = None,
) -> list[dict]:
    if not directory.exists():
        return []
    paths = directory.glob("*.json") if top_only else directory.rglob("*.json")
    entries = []
    for path in paths:
        if path.name.endswith(".partial") or path.name == "current.json":
            continue
        relative_parts = path.relative_to(directory).parts
        if family is None and "runs" in relative_parts:
            continue
        # Grid sidecars are not products of their own; those live next to .f16 files.
        if path.with_suffix(".f16").is_file():
            continue
        try:
            body = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            continue
        valid = body.get("valid_time")
        if not isinstance(valid, str) and isinstance(body.get("time"), str):
            valid = body["time"]
        if family:
            ident = (family + "/" + path.relative_to(directory).as_posix()).replace("/", "-").removesuffix(".json")
        else:
            ident = path.relative_to(root / "products").as_posix().replace("/", "-").removesuffix(".json")
        entries.append({
            "id": ident,
            "path": _rel(root, path),
            "valid_time": valid if isinstance(valid, str) else None,
            "run": body.get("run"),
            "units": body.get("units"),
            "grid": body.get("grid"),
            "stations": body.get("stations"),
            "sha256": sha256(path.read_bytes()),
            **_licence_fields(body, licence),
        })
    return entries


def _published_binary(
    root: Path,
    family: str,
    suffixes: set[str],
    licence: str,
    prefix: str,
    *,
    previous_prefix: str | None = None,
) -> list[dict]:
    from isobar_data.publish import read_pointer, run_dir

    pointer = read_pointer(root, family)
    if not pointer["runs"]:
        return _binary_tree(root, root / "products" / family, suffixes, licence, prefix)
    latest = pointer.get("latest")
    entries = _flat_binary(root, run_dir(root, family, latest), suffixes, licence, prefix) if latest else []
    if previous_prefix:
        prior = [item for item in pointer["runs"] if item != latest]
        if prior:
            entries.extend(_flat_binary(
                root, run_dir(root, family, prior[-1]), suffixes, licence, previous_prefix,
            ))
    return entries


def _flat_binary(root: Path, directory: Path, suffixes: set[str], licence: str, prefix: str) -> list[dict]:
    if not directory.is_dir():
        return []
    entries = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        entries.append({
            "id": f"{prefix}-{path.name}",
            "path": _rel(root, path),
            "valid_time": iso(modified),
            "run": None,
            "units": None,
            "grid": None,
            "stations": None,
            "sha256": sha256(path.read_bytes()),
            "licence_id": licence,
            "licence_ids": [licence],
        })
    return entries


def _binary_tree(root: Path, directory: Path, suffixes: set[str], licence: str, prefix: str) -> list[dict]:
    if not directory.exists():
        return []
    entries = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        ident = prefix + "-" + path.relative_to(directory).as_posix().replace("/", "-")
        entries.append({
            "id": ident,
            "path": _rel(root, path),
            "valid_time": iso(modified),
            "run": None,
            "units": None,
            "grid": None,
            "stations": None,
            "sha256": sha256(path.read_bytes()),
            "licence_id": licence,
            "licence_ids": [licence],
        })
    return entries


def _obs_entry(root: Path, path: Path) -> dict:
    latest = None
    stations = []
    try:
        connection = sqlite3.connect(path)
        row = connection.execute("SELECT MAX(aifstime_utc) FROM obs").fetchone()
        latest = row[0] if row else None
        stations = [item[0] for item in connection.execute("SELECT DISTINCT wmo FROM obs ORDER BY wmo")]
        connection.close()
    except sqlite3.Error:
        pass
    valid = None
    if latest and len(str(latest)) == 14:
        moment = datetime.strptime(str(latest), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        valid = iso(moment)
    return {
        "id": "obs",
        "path": _rel(root, path),
        "valid_time": valid,
        "run": None,
        "units": None,
        "grid": None,
        "stations": stations,
        "sha256": sha256(path.read_bytes()),
        "licence_id": "bom-anonymous-ftp",
        "licence_ids": ["bom-anonymous-ftp"],
    }
