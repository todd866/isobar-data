"""ECMWF Open Data 0.25° crop for the national chart.

00Z and 12Z through 168 h: 3-hourly from 0 to 144 h, then 6-hourly to 168 h.
06Z and 18Z are not published; they end at or before 144 h and would shorten
the week. Gusts, most-unstable CAPE and total cloud cover are stored when the
index lists them. Visibility is not in the open-data set; the 9 km points carry it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import urllib.error

from isobar_data.contract import GLOBAL_GRID_FAMILY, GRID_FAMILY, GRID_SCHEMA_VERSION, marker
from isobar_data.http import Later
from isobar_data.fetch_ecmwf import (
    GRID_EAST,
    GRID_NORTH,
    GRID_SOUTH,
    GRID_STEP,
    GRID_WEST,
    crop_north_up,
    decode_north_up,
    fetch_bytes,
    grid_shape,
    grib_url,
    index_url,
    parse_index,
    run_id,
    select_message,
    to_display_units,
)
from isobar_data.normalise import dump_json, pack_f16
from isobar_data.publish import publish_run, run_dir
from isobar_data.storage import atomic_write

# 3-hourly through 144 h, then the 6-hourly steps ECMWF actually publishes.
# 147 h and the other gaps are not files and are not interpolated here.
LEAD_HOURS = tuple([*range(0, 145, 3), *range(150, 169, 6)])


@dataclass(frozen=True)
class GridSpec:
    family: str
    west: float
    east: float
    north: float
    south: float
    step: float
    nx: int
    ny: int
    wraps_longitude: bool = False
    source_step: float = GRID_STEP


def grid_spec(family: str = GRID_FAMILY) -> GridSpec:
    """Return the published target grid without changing the AU default."""
    if family == GLOBAL_GRID_FAMILY:
        return GridSpec(family, -180.0, 179.5, 90.0, -90.0, 0.5, 720, 361, True)
    if family != GRID_FAMILY:
        raise ValueError(f"unknown grid family {family!r}")
    nx, ny = grid_shape()
    return GridSpec(family, GRID_WEST, GRID_EAST, GRID_NORTH, GRID_SOUTH, GRID_STEP, nx, ny, False)

REQUIRED = (
    ("msl", "sfc", None, "mslp", "hPa"),
    ("t", "pl", "850", "t850", "degC"),
    ("2t", "sfc", None, "t2m", "degC"),
    ("10u", "sfc", None, "u10", "m/s"),
    ("10v", "sfc", None, "v10", "m/s"),
    ("tp", "sfc", None, "tp", "mm"),
)
OPTIONAL = (
    ("10fg", "sfc", None, "gust10", "m/s"),
    ("mucape", "sfc", None, "mucape", "J/kg"),
    ("tcc", "sfc", None, "cloud_cover", "%"),
    ("cape", "sfc", None, "cape", "J/kg"),
    ("vis", "sfc", None, "visibility", "m"),
)


def _long_cycle(run: datetime) -> bool:
    """00Z and 12Z reach past a week. 06Z and 18Z do not."""
    return run.astimezone(timezone.utc).hour in (0, 12)


def cycle_pair(now: datetime) -> tuple[datetime, datetime]:
    """Latest 00Z and latest 12Z at or before ``now``.

    A newer 06Z or 18Z is ignored. Those cycles stop short of 168 h, so
    publishing one would shorten the chart.
    """
    now = now.astimezone(timezone.utc)
    day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    cycles = []
    for hour in (0, 12):
        cycle = day.replace(hour=hour)
        if cycle > now:
            cycle -= timedelta(days=1)
        cycles.append(cycle)
    return cycles[0], cycles[1]


def message_path(raw_dir: Path, run: datetime, step: int, param: str, levelist: str | None) -> Path:
    label = param if levelist is None else f"{param}{levelist}"
    return raw_dir / run_id(run) / f"{step:03d}" / f"{label}.grib"


def required_artifacts(run: datetime, family: str = GRID_FAMILY) -> list[str]:
    names = []
    for step in LEAD_HOURS:
        valid = run_id(run + timedelta(hours=step))
        for _param, _levtype, _levelist, variable, _units in REQUIRED:
            names.append(f"{variable}/{valid}.f16")
            names.append(f"{variable}/{valid}.json")
    return names


def product_paths(root: Path, run: datetime, variable: str, valid: datetime, family: str = GRID_FAMILY) -> tuple[Path, Path]:
    directory = run_dir(root, family, run_id(run)) / variable
    stem = run_id(valid)
    return directory / f"{stem}.f16", directory / f"{stem}.json"


def convert_field(values: np.ndarray, short_name: str, units: str) -> tuple[np.ndarray, str]:
    units = units.strip()
    if short_name == "t":
        return to_display_units(values, "t", units), "degC"
    if short_name in {"msl", "2t", "10u", "10v", "tp"}:
        canonical = {"msl": "hPa", "2t": "degC", "10u": "m/s", "10v": "m/s", "tp": "mm"}[short_name]
        return to_display_units(values, short_name, units), canonical
    if short_name == "10fg":
        if units not in {"m s**-1", "m/s", "m s-1"}:
            raise ValueError(f"10fg units {units!r}")
        return np.asarray(values, dtype=np.float32), "m/s"
    if short_name in {"mucape", "cape"}:
        if units not in {"J kg**-1", "J/kg", "J kg-1"}:
            raise ValueError(f"{short_name} units {units!r}")
        return np.asarray(values, dtype=np.float32), "J/kg"
    if short_name == "tcc":
        if units in {"(0 - 1)", "(0-1)", "0-1", "proportion"}:
            return np.asarray(values, dtype=np.float64) * 100.0, "%"
        if units in {"%", "percent"}:
            return np.asarray(values, dtype=np.float32), "%"
        raise ValueError(f"tcc units {units!r}")
    if short_name == "vis":
        if units not in {"m", "metres", "meters"}:
            raise ValueError(f"vis units {units!r}")
        return np.asarray(values, dtype=np.float32), "m"
    raise ValueError(f"unexpected field {short_name}")


def sidecar(run: datetime, valid: datetime, units: str, param: str, family: str = GRID_FAMILY) -> dict:
    spec = grid_spec(family)
    return {
        "lat0": spec.north,
        "lon0": spec.west,
        "dlat": -spec.step,
        "dlon": spec.step,
        "ny": spec.ny,
        "nx": spec.nx,
        "units": units,
        "run": run.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_time": valid.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": "ecmwf-ifs-0p25-open-data",
        "fill": -32768,
        "lead_hours": int(round((valid - run).total_seconds() / 3600.0)),
        "order": "north-to-south, west-to-east",
        "dtype": "float16",
        "endian": "little",
        "param": param,
        "wraps_longitude": spec.wraps_longitude,
        "source_grid_step": spec.source_step,
        "target_grid_step": spec.step,
        "resampling": "nearest-node",
    }


def _current_sidecar(path: Path, step: int) -> bool:
    """True when a stored sidecar already has this schema's lead hour.

    Runs stored before schema 2 lack lead_hours and carry native_step_hours;
    the readers reject those, so such a field is fetched again rather than
    republished under a schema 2 manifest.
    """
    try:
        side = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(side, dict) and side.get("lead_hours") == step
            and "native_step_hours" not in side)


def _present(entries: list[dict], param: str, levtype: str, levelist: str | None) -> bool:
    try:
        select_message(entries, param, levtype, levelist)
    except LookupError:
        return False
    return True


def _take(consume) -> None:
    if consume is None:
        return
    if not consume():
        raise Later(datetime.now(timezone.utc) + timedelta(minutes=1), "ecmwf bucket is empty")


def _require_space(path: Path) -> None:
    from isobar_data.retain import large_fetch_block

    reason = large_fetch_block(path.parent.parent)
    if reason:
        raise Later(datetime.now(timezone.utc) + timedelta(minutes=30), reason)


def _load_index(raw_dir: Path, run: datetime, step: int, consume=None) -> list[dict]:
    path = raw_dir / run_id(run) / f"{step:03d}" / "messages.index"
    if path.is_file() and path.stat().st_size > 0:
        return parse_index(path.read_text())
    _require_space(raw_dir)
    _take(consume)
    text = fetch_bytes(index_url(run, step)).decode("utf-8")
    atomic_write(path, text.encode())
    return parse_index(text)


def _load_blob(
    raw_dir: Path,
    run: datetime,
    step: int,
    param: str,
    levtype: str,
    levelist: str | None,
    entries: list[dict],
    consume=None,
) -> bytes:
    path = message_path(raw_dir, run, step, param, levelist)
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes()
    _require_space(raw_dir)
    _take(consume)
    row = select_message(entries, param, levtype, levelist)
    blob = fetch_bytes(grib_url(run, step), int(row["_offset"]), int(row["_length"]))
    atomic_write(path, blob)
    return blob


def _write_field(root: Path, run: datetime, valid: datetime, variable: str, param: str, grid: np.ndarray, units: str, family: str = GRID_FAMILY) -> None:
    f16, side = product_paths(root, run, variable, valid, family)
    lead = int(round((valid - run).total_seconds() / 3600.0))
    if f16.is_file() and _current_sidecar(side, lead):
        return
    atomic_write(f16, pack_f16(grid))
    dump_json(side, sidecar(run, valid, units, param, family))


def _incomplete(run: datetime, stored: int, skipped: int, missing_steps: list, detail: str) -> dict:
    return {
        "ok": False,
        "run": run.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stored": stored,
        "skipped": skipped,
        "missing_steps": missing_steps,
        "detail": detail,
        "complete": False,
    }


def _pointer_fields() -> dict:
    return {
        "schema_version": GRID_SCHEMA_VERSION,
        "forecast_hours": list(LEAD_HOURS),
        "horizon_hours": LEAD_HOURS[-1],
        "uniform_step_hours": None,
    }


def _run_manifest(run: datetime, family: str = GRID_FAMILY) -> dict:
    spec = grid_spec(family)
    return {
        **marker(family=family),
        "schema_version": GRID_SCHEMA_VERSION,
        "schema": GRID_SCHEMA_VERSION,
        "forecast_hours": list(LEAD_HOURS),
        "horizon_hours": LEAD_HOURS[-1],
        "uniform_step_hours": None,
        "run": run.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": "ecmwf-ifs-0p25-open-data",
        "source": "ecmwf-open-data",
        "grid": {
            "west": spec.west,
            "east": spec.east,
            "north": spec.north,
            "south": spec.south,
            "step": spec.step,
            "nx": spec.nx,
            "ny": spec.ny,
            "order": "north-to-south, west-to-east",
            "dtype": "float16",
            "endian": "little",
            "wraps_longitude": spec.wraps_longitude,
            "source_grid_step": spec.source_step,
            "target_grid_step": spec.step,
            "resampling": "nearest-node",
        },
    }


def archive_run(root: Path, run: datetime, consume=None, family: str = GRID_FAMILY) -> dict:
    """Fetch any missing week of fields for one 00Z or 12Z cycle.

    Already-stored steps are skipped. A missing listed hour is not published
    and is not filled. 06Z and 18Z are refused so they cannot become current.
    """
    if not _long_cycle(run):
        return _incomplete(
            run, 0, 0, [],
            "06Z/18Z open data ends before 168 h; the published grid uses 00Z/12Z",
        )
    raw_dir = root / "raw" / "ecmwf"
    target = grid_spec(family)
    nx, ny = target.nx, target.ny
    stored = 0
    skipped = 0
    missing_steps = []
    optional_note = ""
    saw_optional = False
    for step in LEAD_HOURS:
        valid = run + timedelta(hours=step)
        pending = []
        for param, levtype, levelist, variable, _units in REQUIRED + OPTIONAL:
            f16, side = product_paths(root, run, variable, valid, family)
            if f16.is_file() and _current_sidecar(side, step):
                skipped += 1
            else:
                pending.append((param, levtype, levelist, variable))
        if not pending:
            continue
        try:
            entries = _load_index(raw_dir, run, step, consume)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Stop. Later steps cannot make a week, and they are not stored
                # as empty frames.
                missing_steps.append(step)
                break
            raise
        if not saw_optional:
            absent = []
            present = []
            for param, levtype, levelist, variable, _units in OPTIONAL:
                if _present(entries, param, levtype, levelist):
                    present.append(variable)
                else:
                    absent.append(param)
            optional_note = "open-data fields " + ", ".join(present)
            if absent:
                optional_note += "; not in the open-data set: " + ", ".join(absent)
            saw_optional = True
            print(f"ecmwf {run_id(run)} {optional_note}", file=sys.stderr)
        fetchable = []
        for param, levtype, levelist, variable in pending:
            spec = _spec(param)
            if spec is None:
                continue
            if spec in OPTIONAL and not _present(entries, param, levtype, levelist):
                skipped += 1
                continue
            fetchable.append((param, levtype, levelist, variable, spec))
        if not fetchable:
            continue
        print(f"ecmwf {run_id(run)} step {step}", file=sys.stderr)
        for param, levtype, levelist, variable, spec in fetchable:
            try:
                blob = _load_blob(raw_dir, run, step, param, levtype, levelist, entries, consume)
                grid, lat0, lon0, lat1, lon1, dlat, dlon, short_name, units = decode_north_up(
                    blob, map_missing_to_nan=True
                )
                display, canonical = convert_field(grid, short_name, units)
                cropped = crop_north_up(display, lat0, lon0, lat1, lon1,
                    west=target.west, east=target.east, north=target.north,
                    south=target.south, step=target.step, dlat=dlat, dlon=dlon)
            except Later:
                raise
            except Exception as exc:
                if spec not in OPTIONAL:
                    missing_steps.append(step)
                optional_note = (optional_note + f"; {param} step {step}: {exc}").strip("; ")
                print(f"ecmwf {param} step {step} failed: {exc}", file=sys.stderr)
                continue
            if cropped.shape != (ny, nx):
                raise RuntimeError(f"{param} crop is {cropped.shape}, expected {(ny, nx)}")
            _write_field(root, run, valid, variable, param, cropped, canonical, family)
            stored += 1
    required = required_artifacts(run, family)
    stage = run_dir(root, family, run_id(run))
    missing = [name for name in required if not (stage / name).is_file()]
    if missing_steps or missing:
        return _incomplete(
            run, stored, skipped, missing_steps,
            optional_note or "incomplete run was not published",
        )
    dump_json(stage / "manifest.json", _run_manifest(run, family))
    publish_run(
        root, family, run_id(run), required + ["manifest.json"],
        # A global run is ~240 MB. Five complete cycles retain the previous
        # 24-hour rain window while allowing older runs to be reclaimed by
        # the existing 8 GB archive budget. AU history keeps its prior policy.
        keep=5 if family == GLOBAL_GRID_FAMILY else "all", pointer_fields=_pointer_fields(),
    )
    return {
        "ok": True,
        "run": run.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stored": stored,
        "skipped": skipped,
        "missing_steps": missing_steps,
        "detail": optional_note,
        "complete": True,
    }


def _spec(param: str):
    for row in REQUIRED + OPTIONAL:
        if row[0] == param:
            return row
    return None


def archive_cycles(root: Path, now: datetime, consume=None) -> dict:
    """Archive latest 00Z/12Z cycles for AU and global target families.

    Both families share the raw ECMWF index/message cache, so the second target
    reuses every downloaded GRIB object from the first pass.
    """
    results = []
    notes = []
    complete = True
    newest = None
    for cycle in cycle_pair(now):
        chosen = cycle
        try:
            _load_index(root / "raw" / "ecmwf", chosen, 0, consume)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            chosen = cycle - timedelta(days=1)
            try:
                _load_index(root / "raw" / "ecmwf", chosen, 0, consume)
            except urllib.error.HTTPError as earlier:
                if earlier.code == 404:
                    notes.append(f"{run_id(cycle)} not on the portal")
                    complete = False
                    continue
                raise
        for family in (GRID_FAMILY, GLOBAL_GRID_FAMILY):
            summary = archive_run(root, chosen, consume, family=family)
            summary["family"] = family
            results.append(summary)
            if summary["detail"]:
                notes.append(f"{family}: {summary['detail']}")
            if not summary["complete"]:
                complete = False
        if newest is None or chosen > newest:
            newest = chosen
    detail = " ".join(dict.fromkeys(notes))
    return {
        "ok": complete and bool(results),
        "complete": complete and bool(results),
        "run": None if newest is None else newest.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detail": detail,
        "stored": sum(item["stored"] for item in results),
        "skipped": sum(item["skipped"] for item in results),
        "cycles": results,
    }
