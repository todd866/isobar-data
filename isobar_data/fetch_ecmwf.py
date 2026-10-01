"""Fetch a cropped ECMWF IFS 0.25° window from the Open Data portal.

The portal publishes one GRIB2 file per forecast step. Each file has a sibling
``.index`` of JSON lines with ``_offset`` and ``_length``. This tool requests
only ``msl``, ``t`` at 850 hPa, ``2t``, ``10u``, ``10v`` and ``tp``, then
writes little-endian float32 grids the Objective-C chart can read. It does not
sample Open-Meteo: a national 0.25° point query would exceed that service's
free daily quota.

Licence of the bytes: ECMWF Open Data, CC BY 4.0.
https://creativecommons.org/licenses/by/4.0/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from isobar_data.contract import marker

from isobar_data.identity import USER_AGENT
PORTAL = "https://data.ecmwf.int/forecasts"
GRID_WEST = 95.0
GRID_EAST = 170.0
GRID_NORTH = 0.0
GRID_SOUTH = -50.0
GRID_STEP = 0.25
# Surface and 850 hPa fields the chart draws. ``t`` is selected on 850 hPa.
PARAMETERS = (
    ("msl", "sfc", None),
    ("t", "pl", "850"),
    ("2t", "sfc", None),
    ("10u", "sfc", None),
    ("10v", "sfc", None),
    ("tp", "sfc", None),
)
FILE_FOR = {
    "msl": "msl.f32",
    "t": "t850.f32",
    "2t": "t2m.f32",
    "10u": "u10.f32",
    "10v": "v10.f32",
    "tp": "tp.f32",
}
EST = timezone(timedelta(hours=10))


def grid_shape() -> tuple[int, int]:
    nx = int(round((GRID_EAST - GRID_WEST) / GRID_STEP)) + 1
    ny = int(round((GRID_NORTH - GRID_SOUTH) / GRID_STEP)) + 1
    return nx, ny


def run_id(run: datetime) -> str:
    run = run.astimezone(timezone.utc)
    return run.strftime("%Y%m%dT%HZ")


def parse_run_id(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%dT%HZ").replace(tzinfo=timezone.utc)


def product_stem(run: datetime, step: int) -> str:
    """Portal path without scheme, for one IFS 0.25° operational step."""
    run = run.astimezone(timezone.utc)
    stamp = run.strftime("%Y%m%d%H") + "0000"
    name = f"{stamp}-{int(step)}h-oper-fc"
    day = run.strftime("%Y%m%d")
    hour = run.strftime("%H")
    return f"{day}/{hour}z/ifs/0p25/oper/{name}"


def index_url(run: datetime, step: int) -> str:
    return f"{PORTAL}/{product_stem(run, step)}.index"


def grib_url(run: datetime, step: int) -> str:
    return f"{PORTAL}/{product_stem(run, step)}.grib2"


def max_step_hours(run: datetime) -> int:
    """00Z and 12Z reach 360 h. 06Z and 18Z stop at 144 h (IFS cycle 50r1)."""
    return 144 if run.astimezone(timezone.utc).hour in (6, 18) else 360


def lead_hours(run: datetime, valid: datetime) -> int:
    seconds = (valid.astimezone(timezone.utc) - run.astimezone(timezone.utc)).total_seconds()
    hours = int(round(seconds / 3600.0))
    if abs(seconds - hours * 3600) > 1:
        raise ValueError(f"{valid.isoformat()} is not a whole hour after {run.isoformat()}")
    if hours < 0 or hours % 3:
        raise ValueError(f"lead {hours} h is not a published 3-hour step")
    return hours


def nearest_step(instant: datetime) -> datetime:
    instant = instant.astimezone(timezone.utc)
    step = 3 * 3600
    rounded = int(round(instant.timestamp() / step)) * step
    return datetime.fromtimestamp(rounded, timezone.utc)


def prognosis_times(now: datetime) -> list[datetime]:
    """Eight Bureau panels: 10:00 Eastern Standard the next day, then every 12 h.

    Eastern Standard is fixed UTC+10, the same rule the chart uses. The live
    sheet is Sunday 10am through Wednesday 10pm when ``now`` falls on Saturday.
    """
    local = now.astimezone(EST)
    issue = local.date()
    first_day = issue + timedelta(days=1)
    first = datetime(first_day.year, first_day.month, first_day.day, 10, tzinfo=EST)
    return [(first + timedelta(hours=12 * i)).astimezone(timezone.utc) for i in range(8)]


def chart_times(now: datetime) -> list[datetime]:
    """Now, snapped to a model step, then the eight prognosis times. No duplicates."""
    times = [nearest_step(now)]
    for valid in prognosis_times(now):
        if valid not in times:
            times.append(valid)
    return times


def rain_source(latest: datetime, valid: datetime) -> tuple[datetime, int, int]:
    """Run and steps whose ``tp`` difference is the 24 h ending at ``valid``.

    ``tp`` accumulates from the start of its own run. A frame in the first day
    of ``latest`` borrows the run 24 h earlier so the window is still 24 h, the
    same definition the later frames use.
    """
    lead = lead_hours(latest, valid)
    if lead >= 24:
        return latest, lead, lead - 24
    earlier = latest - timedelta(hours=24)
    return earlier, lead + 24, lead


def parse_index(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def select_message(entries: list[dict], param: str, levtype: str, levelist: str | None) -> dict:
    matches = []
    for entry in entries:
        if entry.get("param") != param or entry.get("levtype") != levtype:
            continue
        if levelist is not None and str(entry.get("levelist")) != str(levelist):
            continue
        if levelist is None and entry.get("levtype") == "pl":
            continue
        matches.append(entry)
    if len(matches) != 1:
        raise LookupError(f"{param} {levtype} {levelist}: {len(matches)} index rows")
    row = matches[0]
    if "_offset" not in row or "_length" not in row:
        raise LookupError(f"{param} index row has no byte range")
    return row


def crop_north_up(
    grid: np.ndarray,
    lat0: float,
    lon0: float,
    lat1: float,
    lon1: float,
    west: float = GRID_WEST,
    east: float = GRID_EAST,
    north: float = GRID_NORTH,
    south: float = GRID_SOUTH,
    step: float = GRID_STEP,
    dlat: float | None = None,
    dlon: float | None = None,
) -> np.ndarray:
    """Crop a north-up regular lat/lon grid. Columns run east and may wrap at 360°.

    ``grid[0, 0]`` is ``(lat0, lon0)``. IFS 0.25° starts at 180°E, so a longitude
    west of the start is reached by going the long way around. ``dlat`` is negative
    when row 0 is the north edge.
    """
    grid = np.asarray(grid)
    ny_in, nx_in = grid.shape
    if ny_in < 2 or nx_in < 2:
        raise ValueError("grid is too small to crop")
    if dlat is None:
        dlat = (lat1 - lat0) / (ny_in - 1)
    if dlon is None:
        span = (lon1 - lon0) / (nx_in - 1)
        # A full global circle stores the last longitude just west of the first.
        dlon = span if span > 0 else 360.0 / nx_in
    if not dlat < 0 or not dlon > 0:
        raise ValueError(f"expected north-up eastward spacing, got {dlat}, {dlon}")
    nx = int(round((east - west) / step)) + 1
    ny = int(round((north - south) / step)) + 1
    lats = north - np.arange(ny) * step
    lons = west + np.arange(nx) * step
    jj = np.rint((lats - lat0) / dlat).astype(int)
    ii = np.rint(((lons - (lon0 % 360.0)) % 360.0) / dlon).astype(int) % nx_in
    if jj.min() < 0 or jj.max() >= ny_in:
        raise ValueError(f"latitude crop {south}–{north} falls outside {lat1}–{lat0}")
    if abs((lat0 + jj * dlat) - lats).max() > step * 0.05:
        raise ValueError("latitude crop is not on a grid node")
    recon = (lon0 + ii * dlon) % 360.0
    if abs(recon - (lons % 360.0)).max() > step * 0.05:
        raise ValueError("longitude crop is not on a grid node")
    return np.ascontiguousarray(grid[np.ix_(jj, ii)], dtype=np.float32)


def to_display_units(values: np.ndarray, short_name: str, units: str) -> np.ndarray:
    """hPa, °C, m/s, or mm. Unknown units are refused rather than guessed."""
    units = units.strip()
    out = np.asarray(values, dtype=np.float64)
    if short_name == "msl":
        if units == "Pa":
            out = out / 100.0
        elif units not in {"hPa", "millibar"}:
            raise ValueError(f"msl units {units!r}")
    elif short_name in {"t", "2t"}:
        if units == "K":
            out = out - 273.15
        elif units not in {"C", "degC", "Celsius"}:
            raise ValueError(f"{short_name} units {units!r}")
    elif short_name in {"10u", "10v"}:
        if units not in {"m s**-1", "m/s", "m s-1"}:
            raise ValueError(f"{short_name} units {units!r}")
    elif short_name == "tp":
        if units == "m":
            out = out * 1000.0
        elif units in {"kg m**-2", "kg/m2", "mm"}:
            pass
        else:
            raise ValueError(f"tp units {units!r}")
    else:
        raise ValueError(f"unexpected field {short_name}")
    return out.astype(np.float32)


def accumulation_window(end_mm: np.ndarray, start_mm: np.ndarray) -> np.ndarray:
    """Millimetres between two accumulated totals.

    A hundredth of a millimetre below zero is packing noise and becomes zero.
    A real reset (the later total much smaller than the earlier one) stays
    negative so the caller can see it instead of hatching a bogus field.
    """
    diff = np.asarray(end_mm, dtype=np.float32) - np.asarray(start_mm, dtype=np.float32)
    noise = (diff < 0) & (diff > -0.05)
    diff = diff.copy()
    diff[noise] = 0
    return diff


def _request(url: str, range_header: str | None, timeout: float) -> tuple[bytes, int]:
    headers = {"User-Agent": USER_AGENT}
    if range_header:
        headers["Range"] = range_header
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), getattr(response, "status", 200)


def fetch_bytes(url: str, offset: int | None = None, length: int | None = None) -> bytes:
    """GET a URL, or one byte range. 429 and 5xx back off. A 200 that ignores Range is refused."""
    range_header = None
    if offset is not None:
        if length is None or length < 1:
            raise ValueError("a byte range needs a positive length")
        range_header = f"bytes={offset}-{offset + length - 1}"
    delay = 1.5
    last = "no response"
    for attempt in range(7):
        try:
            body, status = _request(url, range_header, timeout=180)
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
            if error.code == 404:
                raise
            if error.code in {429, 500, 502, 503, 504}:
                time.sleep(delay + random.random())
                delay = min(delay * 2, 60)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last = str(error)
            time.sleep(delay + random.random())
            delay = min(delay * 2, 60)
            continue
        if range_header and status == 200 and length is not None and len(body) != length:
            raise RuntimeError(
                f"{url} ignored Range and returned {len(body)} bytes; refusing the full GRIB"
            )
        if range_header and length is not None and len(body) != length:
            raise RuntimeError(f"{url} range returned {len(body)} bytes, expected {length}")
        time.sleep(0.35)
        return body
    raise RuntimeError(f"{url} failed after retries ({last})")


# Resolved at first decode; eccodes >=2.43 supplies its own binary library.
_ECCODES = None


def eccodes_module():
    global _ECCODES
    if _ECCODES is None:
        import eccodes

        _ECCODES = eccodes
    return _ECCODES


def decode_north_up(
    blob: bytes, *, map_missing_to_nan: bool = False
) -> tuple[np.ndarray, float, float, float, float, float, float, str, str]:
    """One GRIB message as a north-up grid, plus original corner coordinates and units."""
    eccodes = eccodes_module()

    gid = eccodes.codes_new_from_message(blob)
    try:
        ni = int(eccodes.codes_get(gid, "Ni"))
        nj = int(eccodes.codes_get(gid, "Nj"))
        lat0 = float(eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees"))
        lon0 = float(eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees"))
        lat1 = float(eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees"))
        lon1 = float(eccodes.codes_get(gid, "longitudeOfLastGridPointInDegrees"))
        i_pos = int(eccodes.codes_get(gid, "iScansPositively"))
        j_pos = int(eccodes.codes_get(gid, "jScansPositively"))
        if map_missing_to_nan:
            try:
                eccodes.codes_set(gid, "missingValue", float("nan"))
            except Exception:
                pass
        values = np.asarray(eccodes.codes_get_values(gid), dtype=np.float64)
        units = str(eccodes.codes_get(gid, "units"))
        short_name = str(eccodes.codes_get(gid, "shortName"))
        dlon = abs(float(eccodes.codes_get(gid, "iDirectionIncrementInDegrees")))
        dlat_abs = abs(float(eccodes.codes_get(gid, "jDirectionIncrementInDegrees")))
    finally:
        eccodes.codes_release(gid)
    if values.size != ni * nj:
        raise RuntimeError(f"{short_name} has {values.size} values, grid is {ni}×{nj}")
    grid = values.reshape(nj, ni)
    # Row 0 becomes the north edge. Column 0 keeps an eastward step, which may wrap.
    if j_pos:
        grid = np.ascontiguousarray(grid[::-1, :])
        lat0 = lat0 + (nj - 1) * dlat_abs
    dlat = -dlat_abs
    if not i_pos:
        grid = np.ascontiguousarray(grid[:, ::-1])
        lon0 = (lon0 - (ni - 1) * dlon) % 360.0
    lat1 = lat0 + (nj - 1) * dlat
    lon1 = (lon0 + (ni - 1) * dlon) % 360.0
    return grid, lat0, lon0, lat1, lon1, dlat, dlon, short_name, units


def _cached_message(raw_dir: Path, run: datetime, step: int, param: str, levelist: str | None) -> Path:
    label = param if levelist is None else f"{param}{levelist}"
    return raw_dir / run_id(run) / f"{int(step):03d}" / f"{label}.grib"


def load_message(
    run: datetime,
    step: int,
    param: str,
    levtype: str,
    levelist: str | None,
    raw_dir: Path,
) -> bytes:
    path = _cached_message(raw_dir, run, step, param, levelist)
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes()
    index = parse_index(fetch_bytes(index_url(run, step)).decode("utf-8"))
    row = select_message(index, param, levtype, levelist)
    blob = fetch_bytes(grib_url(run, step), int(row["_offset"]), int(row["_length"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".grib.partial")
    temporary.write_bytes(blob)
    temporary.replace(path)
    return blob


def field_mm_or_value(blob: bytes, expect: str) -> np.ndarray:
    grid, lat0, lon0, lat1, lon1, dlat, dlon, short_name, units = decode_north_up(blob)
    if short_name != expect:
        raise RuntimeError(f"expected {expect}, GRIB shortName is {short_name}")
    display = to_display_units(grid, short_name, units)
    return crop_north_up(display, lat0, lon0, lat1, lon1, dlat=dlat, dlon=dlon)


def candidate_runs(now: datetime) -> list[datetime]:
    now = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hour = (now.hour // 6) * 6
    cursor = now.replace(hour=hour)
    if cursor > now:
        cursor -= timedelta(hours=6)
    return [cursor - timedelta(hours=6 * i) for i in range(8)]


def index_available(run: datetime, step: int) -> bool:
    try:
        fetch_bytes(index_url(run, step))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise
    return True


def choose_run(now: datetime, last_valid: datetime) -> datetime:
    for run in candidate_runs(now):
        try:
            lead = lead_hours(run, last_valid)
        except ValueError:
            continue
        if lead > max_step_hours(run):
            continue
        print(f"probe {run_id(run)} step {lead}", file=sys.stderr)
        if index_available(run, 0) and index_available(run, lead):
            return run
    raise RuntimeError("no IFS 0.25° run on the portal covers the last prognosis panel")


def _write_f32(path: Path, stack: np.ndarray) -> str:
    data = np.ascontiguousarray(stack, dtype="<f4").tobytes()
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(data)
    temporary.replace(path)
    return hashlib.sha256(data).hexdigest()


def build(now: datetime, out_root: Path, run: datetime | None = None) -> Path:
    times = chart_times(now)
    if run is None:
        run = choose_run(now, times[-1])
    print(f"run {run_id(run)}  {len(times)} valid times", file=sys.stderr)
    for valid in times:
        lead = lead_hours(run, valid)
        if lead > max_step_hours(run):
            raise RuntimeError(f"{valid.isoformat()} is step {lead}, past this cycle")
    raw_dir = out_root / "_raw"
    nx, ny = grid_shape()
    stacks = {name: [] for name, _, _ in PARAMETERS}
    rain_frames = []
    rain_meta = []
    for valid in times:
        lead = lead_hours(run, valid)
        print(f"valid {valid.strftime('%Y-%m-%dT%H:%MZ')} step {lead}", file=sys.stderr)
        for param, levtype, levelist in PARAMETERS:
            blob = load_message(run, lead, param, levtype, levelist, raw_dir)
            cropped = field_mm_or_value(blob, param)
            if cropped.shape != (ny, nx):
                raise RuntimeError(f"{param} crop is {cropped.shape}, expected {(ny, nx)}")
            stacks[param].append(cropped)
        rain_run, step_end, step_start = rain_source(run, valid)
        end = field_mm_or_value(load_message(rain_run, step_end, "tp", "sfc", None, raw_dir), "tp")
        start = field_mm_or_value(load_message(rain_run, step_start, "tp", "sfc", None, raw_dir), "tp")
        window = accumulation_window(end, start)
        if np.nanmedian(window) < -0.2:
            raise RuntimeError(
                f"24 h precipitation at {valid.isoformat()} went backwards; "
                "tp may have reset inside the window"
            )
        rain_frames.append(window)
        rain_meta.append({
            "valid": valid.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run": run.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if rain_run == run
            else rain_run.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "step_end": step_end,
            "step_start": step_start,
            "definition": "tp(step_end) - tp(step_start), mm",
        })
    dest = out_root / run_id(run)
    dest.mkdir(parents=True, exist_ok=True)
    files = {}
    for param, _, _ in PARAMETERS:
        stack = np.stack(stacks[param], axis=0)
        digest = _write_f32(dest / FILE_FOR[param], stack)
        files[param] = {"file": FILE_FOR[param], "sha256": digest}
    rain_digest = _write_f32(dest / "rain24.f32", np.stack(rain_frames, axis=0))
    manifest = {
        **marker(family="grids/ecmwf_ifs025"),
        "schema": 1,
        "source": "ecmwf-open-data",
        "model": "ifs",
        "resolution": "0p25",
        "licence": "CC BY 4.0",
        "licence_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "ECMWF Open Data",
        "portal": "https://data.ecmwf.int/forecasts/",
        "run": run.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grid": {
            "west": GRID_WEST,
            "east": GRID_EAST,
            "north": GRID_NORTH,
            "south": GRID_SOUTH,
            "step": GRID_STEP,
            "nx": nx,
            "ny": ny,
            "order": "time, north-to-south, west-to-east",
            "dtype": "float32",
            "endian": "little",
        },
        "variables": {
            "msl": {**files["msl"], "units": "hPa"},
            "t850": {**files["t"], "units": "degC"},
            "t2m": {**files["2t"], "units": "degC"},
            "u10": {**files["10u"], "units": "m/s"},
            "v10": {**files["10v"], "units": "m/s"},
            "tp": {**files["tp"], "units": "mm", "accumulation": "since-run-start"},
            "rain24": {
                "file": "rain24.f32",
                "sha256": rain_digest,
                "units": "mm",
                "window": "24 h ending at the valid time",
            },
        },
        "times": [v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for v in times],
        "rain24": rain_meta,
    }
    manifest_path = dest / "manifest.json"
    encoded = json.dumps(manifest, indent=2).encode()
    temporary = manifest_path.with_suffix(".json.partial")
    temporary.write_bytes(encoded)
    temporary.replace(manifest_path)
    latest = {**marker(family="grids/ecmwf_ifs025"), "run": run_id(run), "path": run_id(run)}
    latest_path = out_root / "latest.json"
    latest_path.write_text(json.dumps(latest, indent=2) + "\n")
    print(f"wrote {dest}", file=sys.stderr)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crop ECMWF IFS 0.25° Open Data for Isobar.")
    parser.add_argument("--out", default=str(Path.home() / "Data/isobar/ecmwf"), help="output root")
    parser.add_argument("--now", default="", help="UTC instant, yyyy-mm-ddTHH:MM:SSZ (default: current time)")
    parser.add_argument("--run", default="", help="force a cycle, yyyymmddTHHz")
    args = parser.parse_args(argv)
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    else:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    forced = parse_run_id(args.run) if args.run else None
    build(now, Path(args.out), forced)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
