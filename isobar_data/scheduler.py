"""One short-lived pass: whatever is due, then exit.

Sleep skips launchd intervals. The next start catches up the latest cycle only.
"""

from __future__ import annotations

import fcntl
import json
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from isobar_data.aviation_feed import fetch_metar_taf, fetch_pirep, fetch_sigmet, write_aerodrome_files
from isobar_data.publish import published_path
from isobar_data.bom import BureauBlock, connect_ftp, sync_charts, sync_obs, sync_warnings
from isobar_data.config import Config
from isobar_data.grid import archive_cycles
from isobar_data.http import BACKOFF_CAP, Blocked, Http, Later, Stats
from isobar_data.kite_job import write_kite_files
from isobar_data.ledger import (
    build_manifest,
    failed_source,
    healthy_source,
    parse_iso,
    write_attribution,
    write_manifest,
    write_status,
)
from isobar_data.openmeteo import (
    ENSEMBLE,
    FORECAST,
    MARINE,
    MARINE_HOURLY,
    MARINE_METAS,
    META,
    SURFACE_DAILY,
    SURFACE_HOURLY,
    fetch_job,
    upper_hourly,
)
from isobar_data.retain import (
    FREE_SPACE_FLOOR,
    free_space_floor_bytes,
    free_space_floor_label,
    STORE_BUDGET_BYTES,
    ensure_root,
    retain,
    volume_free_bytes as _volume_free_bytes,
)
from isobar_data.runways import fetch_runways
from isobar_data.storage import directory_size
from isobar_data.tokens import load_buckets

CHUNK_GAP = timedelta(minutes=1)
CIRCUIT_THRESHOLD = 5
BACKOFF_BASE = 60


def volume_free_bytes(path: Path) -> int:
    return _volume_free_bytes(path)


def fetch_blocked(root: Path, *, large: bool = False) -> str | None:
    """Refuse a pull that would pass the store cap or the free-space floor.

    ECMWF is large: one open-data object is held back unless the store can
    absorb it without crossing 8 GB. The check runs before the source starts.
    """
    from isobar_data import retain as store

    floor = free_space_floor_bytes()
    if volume_free_bytes(root) < floor:
        return f"volume free space is below {free_space_floor_label()} GB"
    if large:
        return store.large_fetch_block(root)
    if directory_size(root) >= STORE_BUDGET_BYTES:
        return "store is at the 8 GB cap"
    return None


def _backoff_seconds(failures: int) -> float:
    return float(min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, failures - 1))))


def _record_failure(entry: dict, now: datetime, requested: float) -> None:
    failures = int(entry.get("failures") or 0) + 1
    entry["failures"] = failures
    delay = max(float(requested), _backoff_seconds(failures))
    stamp = (now + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["not_before"] = stamp
    if failures >= CIRCUIT_THRESHOLD:
        entry["circuit_open_until"] = stamp


def _circuit_open(entry: dict, now: datetime) -> bool:
    until = entry.get("circuit_open_until")
    return bool(until) and now < parse_iso(until)


def select_catchup(available_newest_first: list[str], stored: set[str]) -> list[str]:
    """The newest run, and the one before it if that one is missing. Never a weekend."""
    chosen = []
    for run in available_newest_first[:2]:
        if run not in stored:
            chosen.append(run)
    return chosen


def open_meteo_runs(current_init: str | None, stored_init: str | None) -> list[str]:
    """The forecast endpoint only serves the current run. Do not walk older inits."""
    if current_init and current_init != stored_init:
        return [current_init]
    return []


@dataclass
class Source:
    id: str
    label: str
    cadence: timedelta
    weight: float
    bucket: str | None
    cycle: bool
    always: bool
    run: object


def is_due(entry: dict, now: datetime, cadence: timedelta, *, always: bool) -> bool:
    if always:
        return True
    not_before = entry.get("not_before")
    if not_before and now < parse_iso(not_before):
        return False
    last = entry.get("last_success")
    if last and now < parse_iso(last) + cadence:
        return False
    return True


def _refresh(previous: dict, source: Source, now: datetime) -> dict:
    run = previous.get("run")
    if previous.get("ok"):
        return healthy_source(
            source_id=source.id,
            name=source.label,
            now=now,
            run=run,
            available=previous.get("available"),
            detail=previous.get("detail") or "",
            cycle=source.cycle,
        )
    return failed_source(
        previous,
        source_id=source.id,
        name=source.label,
        now=now,
        detail=previous.get("detail") or "",
        cycle=source.cycle,
    )


def run_sources(sources: list[Source], ctx: dict) -> list[dict]:
    now = ctx["now"]
    state = ctx["state"]
    status_rows = []
    reports = []
    blocked_bom = False
    for source in sources:
        entry = state.setdefault("sources", {}).setdefault(source.id, {})
        previous = entry.get("status")
        if source.bucket == "bom-ftp" and blocked_bom:
            row = failed_source(
                previous,
                source_id=source.id,
                name=source.label,
                now=now,
                detail="Bureau FTP stopped after a block",
                cycle=source.cycle,
            )
            entry["status"] = row
            status_rows.append(row)
            continue
        if _circuit_open(entry, now):
            if previous:
                status_rows.append(_refresh(previous, source, now))
            reports.append({"id": source.id, "skipped": "circuit"})
            _save_state(ctx["root"], state, ctx["buckets"])
            continue
        large = source.id == "ecmwf-open-data"
        blocked = fetch_blocked(ctx["root"], large=large) if ctx.get("root") is not None else None
        if blocked:
            reports.append({"id": source.id, "skipped": blocked})
            if "8 GB" in blocked:
                row = failed_source(
                    previous,
                    source_id=source.id,
                    name=source.label,
                    now=now,
                    detail=blocked,
                    cycle=source.cycle,
                )
                entry["status"] = row
                status_rows.append(row)
            elif previous:
                status_rows.append(_refresh(previous, source, now))
            continue
        if not is_due(entry, now, source.cadence, always=source.always):
            if previous:
                status_rows.append(_refresh(previous, source, now))
            reports.append({"id": source.id, "skipped": "cadence"})
            continue
        if source.bucket and source.weight > 0:
            bucket = ctx["buckets"][source.bucket]
            if not bucket.try_consume(source.weight, now.timestamp()):
                when = now + timedelta(seconds=max(bucket.retry_after(source.weight, now.timestamp()), 60))
                entry["not_before"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
                row = healthy_source(
                    source_id=source.id,
                    name=source.label,
                    now=now,
                    run=None if not previous else previous.get("run"),
                    available=None if not previous else previous.get("available"),
                    detail="waiting for the token bucket",
                    cycle=source.cycle,
                )
                # A deferred pull is not a failure, and it did not consume the token.
                entry["status"] = previous or row
                status_rows.append(entry["status"])
                reports.append({"id": source.id, "skipped": "bucket", "not_before": entry["not_before"]})
                continue
        try:
            result = source.run(ctx)
            if not isinstance(result, dict):
                result = {"ok": True, "complete": True, "detail": ""}
            succeeded = bool(result.get("complete", True)) and result.get("ok", True) is not False
            if succeeded:
                run = result.get("run") or (previous or {}).get("run")
                available = result.get("available") or (previous or {}).get("available")
                row = healthy_source(
                    source_id=source.id,
                    name=source.label,
                    now=now,
                    run=run,
                    available=available,
                    detail=result.get("detail") or "",
                    cycle=source.cycle,
                )
                entry["last_success"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                entry["failures"] = 0
                entry.pop("circuit_open_until", None)
                entry.pop("not_before", None)
            else:
                detail = result.get("detail") or "incomplete run was not published"
                row = failed_source(
                    previous,
                    source_id=source.id,
                    name=source.label,
                    now=now,
                    detail=detail,
                    cycle=source.cycle,
                )
                requested = 0.0
                if result.get("not_before"):
                    requested = max(0.0, (parse_iso(result["not_before"]) - now).total_seconds())
                _record_failure(entry, now, requested)
            entry["status"] = row
            status_rows.append(row)
            reports.append({"id": source.id, **{k: v for k, v in result.items() if k != "cycles"}})
        except Later as later:
            if later.failure:
                requested = max(0.0, (later.when - now).total_seconds())
                _record_failure(entry, now, requested)
            else:
                entry["not_before"] = later.when.strftime("%Y-%m-%dT%H:%M:%SZ")
            row = healthy_source(
                source_id=source.id,
                name=source.label,
                now=now,
                run=None if not previous else previous.get("run"),
                available=None if not previous else previous.get("available"),
                detail=later.reason,
                cycle=source.cycle,
            )
            entry["status"] = row
            status_rows.append(row)
            reports.append({"id": source.id, "later": later.reason})
        except BureauBlock as exc:
            blocked_bom = True
            entry["not_before"] = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = failed_source(previous, source_id=source.id, name=source.label, now=now, detail=str(exc), cycle=source.cycle)
            entry["status"] = row
            status_rows.append(row)
            reports.append({"id": source.id, "error": str(exc)})
        except (Blocked, Exception) as exc:
            traceback.print_exc()
            row = failed_source(previous, source_id=source.id, name=source.label, now=now, detail=str(exc), cycle=source.cycle)
            _record_failure(entry, now, 0)
            entry["status"] = row
            status_rows.append(row)
            reports.append({"id": source.id, "error": str(exc)})
        _save_state(ctx["root"], state, ctx["buckets"])
    ctx["status_rows"] = status_rows
    return reports


def build_sources(config: Config, *, profile: str = "default") -> list[Source]:
    sources = [
        Source("bom-obs", "BoM obs", timedelta(minutes=15), 0, "bom-ftp", False, False, _bom_obs),
        Source("bom-charts", "BoM charts", timedelta(minutes=15), 0, "bom-ftp", False, False, _bom_charts),
        Source("bom-warnings", "BoM warnings", timedelta(minutes=15), 0, "bom-ftp", False, False, _bom_warnings),
        Source("open-meteo-ifs", "IFS 9 km", timedelta(minutes=15), 0, None, True, False, _surface),
        Source("open-meteo-upper", "Upper air", timedelta(minutes=15), 0, None, True, False, _upper),
        Source("open-meteo-marine", "Marine", timedelta(minutes=15), 0, None, True, False, _marine),
        Source("open-meteo-ensemble", "Ensemble", timedelta(minutes=15), 0, None, True, False, _ensemble),
        Source("ourairports", "Runways", timedelta(days=7), 0, None, False, False, _runways),
        Source("aviation-metar", "METAR", timedelta(minutes=5), 0, None, False, False, _metar),
        Source("aviation-taf", "TAF", timedelta(minutes=15), 0, None, False, False, _taf),
        Source("aviation-sigmet", "SIGMET", timedelta(minutes=5), 0, None, False, False, _sigmet),
        Source("aviation-pirep", "PIREP", timedelta(hours=1), 0, None, False, False, _pirep),
        Source("aviation-files", "Aviation", timedelta(0), 0, None, False, True, _aviation_files),
        Source("kite", "Kite", timedelta(0), 0, None, True, True, _kite),
        Source("ecmwf-open-data", "ECMWF", timedelta(minutes=15), 0, None, True, False, _ecmwf),
    ]
    if profile == "default":
        return sources
    if profile == "public-web":
        allowed = {"open-meteo-ifs", "open-meteo-marine", "ecmwf-open-data"}
        return [source for source in sources if source.id in allowed]
    raise ValueError(f"unknown source profile: {profile}")


def run_once(root: Path, config: Config, now: datetime | None = None, *, profile: str = "default", **http_kwargs) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ensure_root(root)
    # Validate the deployment override before opening the scheduler or making
    # any source request. The default remains the laptop-safe 20 GB floor.
    free_space_floor_bytes()
    lock = _lock(root)
    if lock is None:
        return {"locked": True, "data_dir": str(root)}
    started = datetime.now(timezone.utc)
    try:
        state = _load_state(root)
        buckets = load_buckets(state.get("buckets"))
        stats = Stats()
        http = Http(buckets, stats, now=lambda: now, **http_kwargs)
        ctx = {
            "root": root,
            "config": config,
            "now": now,
            "state": state,
            "buckets": buckets,
            "stats": stats,
            "http": http,
            "ftp_factory": http_kwargs.get("ftp_factory", connect_ftp),
        }
        try:
            sources = build_sources(config) if profile == "default" else build_sources(config, profile=profile)
            reports = run_sources(sources, ctx)
            retention = retain(root, now)
            status = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": ctx.get("status_rows") or []}
            if not retention["within_budget"] or retention.get("detail"):
                status["disk"] = {
                    "bytes": retention["bytes"],
                    "budget_bytes": retention["budget_bytes"],
                    "within_budget": retention["within_budget"],
                    "detail": retention["detail"],
                }
            write_status(root, status)
            write_attribution(root)
            write_manifest(root, build_manifest(root, now))
            _save_state(root, state, buckets)
        finally:
            http.close()
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return {
            "locked": False,
            "data_dir": str(root),
            "elapsed_s": round(elapsed, 3),
            "open_meteo_calls": stats.open_meteo_calls,
            "http_requests": stats.http,
            "bytes_downloaded": stats.bytes,
            "bytes_on_disk": directory_size(root),
            "ftp_downloads": stats.ftp_downloads,
            "ftp_skipped": stats.ftp_skipped,
            "by_host": stats.by_host,
            "sources": reports,
            "retention_removed": len(retention["removed"]),
        }
    finally:
        lock.close()


def _bom_obs(ctx):
    return _with_ftp(ctx, lambda session: _count_ftp(ctx, sync_obs(
        ctx["root"], session, ctx["config"].bom_obs, ctx["state"]
    )))


def _bom_charts(ctx):
    return _with_ftp(ctx, lambda session: _count_ftp(ctx, sync_charts(
        ctx["root"], session, ctx["config"].bom_charts, ctx["state"]
    )))


def _bom_warnings(ctx):
    return _with_ftp(ctx, lambda session: _count_ftp(ctx, sync_warnings(ctx["root"], session, ctx["state"])))


def _count_ftp(ctx, result: dict) -> dict:
    ctx["stats"].ftp_downloads += int(result.get("downloaded") or 0)
    ctx["stats"].ftp_skipped += int(result.get("skipped") or 0)
    detail = []
    if result.get("downloaded"):
        detail.append(f"downloaded {result['downloaded']}")
    if result.get("skipped"):
        detail.append(f"unchanged {result['skipped']}")
    if result.get("titles"):
        detail.append("; ".join(item.get("title") or item.get("id") for item in result["titles"]))
    return {
        "ok": True,
        "complete": True,
        "detail": ", ".join(detail),
        "run": result.get("run"),
        "available": result.get("run"),
    }


def _with_ftp(ctx, fn):
    bucket = ctx["buckets"]["bom-ftp"]
    if not bucket.try_consume(1, time.time()):
        raise Later(ctx["now"] + timedelta(minutes=1), "bom-ftp bucket is empty")
    factory = ctx.get("ftp_factory") or connect_ftp
    session = factory()
    try:
        return fn(session)
    finally:
        session.close()


def _points(config: Config, ensemble: bool = False):
    if ensemble:
        return [(point.id, point.latitude, point.longitude) for point in config.ensemble]
    return [(point.id, point.latitude, point.longitude) for point in config.surface]


def _apply_coords(config: Config, state: dict):
    points = []
    for point in config.ensemble:
        cached = (state.get("coords") or {}).get(str(point.wmo))
        if cached and cached.get("lat") is not None:
            points.append((point.id, float(cached["lat"]), float(cached["lon"])))
        else:
            points.append((point.id, point.latitude, point.longitude))
    return points


def _surface(ctx):
    config = ctx["config"]
    points = _points(config)
    return fetch_job(
        ctx["http"], ctx["root"], ctx["state"], ctx["now"],
        job_id="open-meteo-ifs",
        endpoint=FORECAST,
        meta_urls=(META["ecmwf_ifs"],),
        points=points,
        hourly=SURFACE_HOURLY,
        daily=SURFACE_DAILY,
        model="ecmwf_ifs",
        hours=72,
        days=None,
        cell="nearest",
        wind_kn=True,
        elevation_nan=True,
        native_step=1,
        kind="surface",
        product_dir=ctx["root"] / "products" / "points" / "ecmwf_ifs",
    )


def _upper(ctx):
    config = ctx["config"]
    points = [(item.icao, item.latitude, item.longitude) for item in config.aerodromes]
    return fetch_job(
        ctx["http"], ctx["root"], ctx["state"], ctx["now"],
        job_id="open-meteo-upper",
        endpoint=FORECAST,
        meta_urls=(META["ecmwf_ifs025"],),
        points=points,
        hourly=upper_hourly(),
        model="ecmwf_ifs025",
        hours=72,
        days=None,
        cell="nearest",
        wind_kn=True,
        elevation_nan=True,
        native_step=3,
        kind="upper",
        product_dir=ctx["root"] / "products" / "points" / "ecmwf_ifs025_upper",
    )


def _marine(ctx):
    points = [(point.id, point.latitude, point.longitude) for point in ctx["config"].marine_points]
    return fetch_job(
        ctx["http"], ctx["root"], ctx["state"], ctx["now"],
        job_id="open-meteo-marine",
        endpoint=MARINE,
        meta_urls=MARINE_METAS,
        points=points,
        hourly=MARINE_HOURLY,
        model=None,
        hours=72,
        days=None,
        cell="sea",
        wind_kn=False,
        elevation_nan=False,
        native_step=1,
        kind="marine",
        product_dir=ctx["root"] / "products" / "points" / "marine",
    )


def _ensemble(ctx):
    points = _apply_coords(ctx["config"], ctx["state"])
    return fetch_job(
        ctx["http"], ctx["root"], ctx["state"], ctx["now"],
        job_id="open-meteo-ensemble",
        endpoint=ENSEMBLE,
        meta_urls=(META["ecmwf_ifs025_ensemble"],),
        points=points,
        hourly=("pressure_msl", "temperature_850hPa"),
        model="ecmwf_ifs025_ensemble",
        hours=None,
        days=7,
        cell="nearest",
        wind_kn=False,
        elevation_nan=True,
        native_step=3,
        kind="ensemble",
        product_dir=ctx["root"] / "products" / "ensemble",
    )


def _runways(ctx):
    icaos = [item.icao for item in ctx["config"].aerodromes]
    return fetch_runways(ctx["http"], ctx["root"], ctx["state"], icaos, ctx["now"])


def _metar(ctx):
    icaos = [item.icao for item in ctx["config"].aerodromes]
    return fetch_metar_taf(ctx["http"], ctx["root"], ctx["state"], icaos, which="metar")


def _taf(ctx):
    icaos = [item.icao for item in ctx["config"].aerodromes]
    return fetch_metar_taf(ctx["http"], ctx["root"], ctx["state"], icaos, which="taf")


def _sigmet(ctx):
    return fetch_sigmet(ctx["http"], ctx["root"], ctx["state"])


def _pirep(ctx):
    return fetch_pirep(ctx["http"], ctx["root"], ctx["state"])


def _aviation_files(ctx):
    icaos = [item.icao for item in ctx["config"].aerodromes]
    mapping = {item.icao: item.surface_id for item in ctx["config"].aerodromes}
    write_aerodrome_files(ctx["root"], icaos, mapping)
    newest = None
    for icao in icaos:
        path = published_path(ctx["root"], "aviation", f"{icao}.json")
        if path is None:
            continue
        metar = (json.loads(path.read_text()).get("metar") or {})
        stamp = metar.get("time")
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    return {"ok": True, "complete": True, "detail": ", ".join(icaos), "run": newest}


def _kite(ctx):
    return write_kite_files(ctx["root"], ctx["config"])


def _ecmwf(ctx):
    bucket = ctx["buckets"]["ecmwf-open-data"]

    def consume() -> bool:
        return bucket.try_consume(1, time.time())

    summary = archive_cycles(ctx["root"], ctx["now"], consume)
    if not summary.get("complete"):
        summary["not_before"] = (ctx["now"] + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return summary


def _load_state(root: Path) -> dict:
    path = root / "state.json"
    if not path.is_file():
        return {"buckets": {}, "sources": {}, "ftp": {}, "coords": {}, "validators": {}, "metas": {}}
    return json.loads(path.read_text())


def _save_state(root: Path, state: dict, buckets) -> None:
    from isobar_data.tokens import dump_buckets

    state["buckets"] = dump_buckets(buckets)
    temporary = root / "state.json.partial"
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(root / "state.json")


def _lock(root: Path):
    handle = (root / ".lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle
