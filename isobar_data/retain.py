"""Drop aged raw files, thin the old grids, and prune observation rows."""

from __future__ import annotations

import shutil
import sqlite3
import os
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path

MARKER = ".isobar-root"
THIN_VARS = {"mslp", "t850"}
STORE_BUDGET_BYTES = 8 * 1024**3
FREE_SPACE_FLOOR = 20 * 1024**3
FREE_SPACE_FLOOR_ENV = "ISOBAR_FREE_SPACE_FLOOR_GB"
MIN_FREE_SPACE_FLOOR_GB = Decimal("2")
# One ECMWF open-data step is about 129 MB. Hold the next object unless it fits.
LARGE_FETCH_RESERVE = 256 * 1024**2
PROTECTED_NAMES = {
    ".isobar-root",
    "status.json",
    "manifest.json",
    "attribution.json",
    "state.json",
    "current.json",
    ".lock",
}


class RetentionError(RuntimeError):
    pass


def free_space_floor_gb() -> Decimal:
    raw = os.environ.get(FREE_SPACE_FLOOR_ENV)
    if raw is None:
        return Decimal("20")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{FREE_SPACE_FLOOR_ENV} must be a finite number >= 2") from exc
    if not value.is_finite() or value < MIN_FREE_SPACE_FLOOR_GB:
        raise ValueError(f"{FREE_SPACE_FLOOR_ENV} must be a finite number >= 2")
    return value


def free_space_floor_bytes() -> int:
    return int(free_space_floor_gb() * Decimal(1024**3))


def free_space_floor_label() -> str:
    return format(free_space_floor_gb().normalize(), "f")


def ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if not marker.exists():
        marker.write_text("isobar-data\n")


def volume_free_bytes(path: Path) -> int:
    target = path if path.exists() else path.parent
    if not target.exists():
        target = Path("/")
    return shutil.disk_usage(target).free


def retain(root: Path, now: datetime, *, budget_bytes: int | None = None) -> dict:
    root = root.resolve()
    if not (root / MARKER).is_file():
        raise RetentionError(f"{root} is not an isobar data directory")
    budget = STORE_BUDGET_BYTES if budget_bytes is None else budget_bytes
    removed = []
    removed += _raw(root / "raw" / "open-meteo", now - timedelta(days=14))
    removed += _raw(root / "raw" / "aviation", now - timedelta(days=14))
    removed += _raw(root / "raw" / "warnings", now - timedelta(days=14))
    removed += _raw(root / "raw" / "charts", now - timedelta(days=14))
    removed += _raw(root / "raw" / "ecmwf", now - timedelta(hours=48))
    removed += _raw(root / "raw" / "ensemble", now - timedelta(days=14))
    removed += _raw(root / "raw" / "bom", now - timedelta(days=3))
    removed += _partials(root, now - timedelta(hours=1))
    removed += _grids(root / "products" / "grids" / "ecmwf_ifs025", now)
    removed += _dated(root / "products" / "ensemble", now - timedelta(days=365), keep_latest=True)
    pruned = _obs(root / "products" / "obs" / "obs.sqlite", now - timedelta(days=365))
    history_rows = _history(root / "products" / "aviation" / "history.sqlite", now - timedelta(days=14))
    removed += _enforce_budget(root, budget)
    size = _directory_size(root)
    within = size <= budget
    detail = ""
    if not within:
        detail = "8 GB cap cannot be met by deleting eligible files"
    elif budget - size < LARGE_FETCH_RESERVE:
        detail = "not enough room under the 8 GB cap for an ECMWF fetch"
    return {
        "removed": removed,
        "obs_rows": pruned,
        "history_rows": history_rows,
        "bytes": size,
        "budget_bytes": budget,
        "within_budget": within,
        "detail": detail,
    }


def large_fetch_block(root: Path) -> str | None:
    """None when a large object can land without crossing the store cap.

    Eligible files are deleted first. If what remains is still over the cap,
    or the leftover room is smaller than one ECMWF object, the fetch must not start.
    """
    floor = free_space_floor_bytes()
    if volume_free_bytes(root) < floor:
        return f"volume free space is below {free_space_floor_label()} GB"
    budget = STORE_BUDGET_BYTES
    _enforce_budget(root, budget)
    size = _directory_size(root)
    if size > budget:
        return "8 GB cap cannot be met by deleting eligible files"
    if budget - size < LARGE_FETCH_RESERVE:
        return "not enough room under the 8 GB cap for an ECMWF fetch"
    return None


def _directory_size(root: Path) -> int:
    from isobar_data.storage import directory_size

    return directory_size(root)


def rotate_log(path: Path, *, max_bytes: int = 1_048_576, backups: int = 3) -> bool:
    """Rename a launchd log once it passes ``max_bytes``. Keep ``backups`` generations."""
    if backups < 1 or not path.is_file() or path.stat().st_size <= max_bytes:
        return False
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists() or oldest.is_symlink():
        oldest.unlink()
    for index in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        if src.exists() or src.is_symlink():
            src.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    return True


def _partials(root: Path, cutoff: datetime) -> list[str]:
    removed = []
    for path in list(root.rglob("*")):
        if not path.is_file() or not path.name.endswith(".partial"):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed.append(str(path))
    return removed


def _history(path: Path, cutoff: datetime) -> int:
    if not path.is_file():
        return 0
    stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    connection = sqlite3.connect(path)
    try:
        cursor = connection.execute(
            "DELETE FROM metar WHERE obs_time != '' AND obs_time < ?",
            (stamp,),
        )
        connection.commit()
        return cursor.rowcount
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _enforce_budget(root: Path, budget: int) -> list[str]:
    from isobar_data.publish import read_pointer, run_dir
    from isobar_data.storage import directory_size

    protected = set()
    products = root / "products"
    if products.is_dir():
        for pointer in products.rglob("current.json"):
            family = pointer.parent.relative_to(products).as_posix()
            for run_id in read_pointer(root, family)["runs"]:
                protected.add(run_dir(root, family, run_id).resolve())
    removed = []
    while directory_size(root) > budget:
        victim = _oldest_deletable(root, protected)
        if victim is None:
            break
        victim.unlink()
        removed.append(str(victim))
    return removed


def _oldest_deletable(root: Path, protected: set[Path]) -> Path | None:
    oldest = None
    oldest_mtime = None
    for path in root.rglob("*"):
        if not path.is_file() or path.name in PROTECTED_NAMES:
            continue
        relative = path.relative_to(root)
        in_raw = bool(relative.parts) and relative.parts[0] in {"raw", "staging"}
        if not in_raw and "runs" not in relative.parts:
            continue
        resolved = path.resolve()
        if any(_inside(resolved, directory) for directory in protected):
            continue
        modified = path.stat().st_mtime
        if oldest is None or modified < oldest_mtime:
            oldest = path
            oldest_mtime = modified
    return oldest


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _raw(directory: Path, cutoff: datetime) -> list[str]:
    if not directory.exists():
        return []
    removed = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.name.endswith(".partial"):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed.append(str(path))
    _prune_empty(directory)
    return removed


def _grids(directory: Path, now: datetime) -> list[str]:
    """Full lead times last 14 days. MSLP and 850 hPa then live in ``thin/`` for a year."""
    if not directory.exists():
        return []
    removed = []
    full_cutoff = now - timedelta(days=14)
    year_cutoff = now - timedelta(days=365)
    thin = directory / "thin"
    full = [path for path in directory.rglob("*.f16") if "thin" not in path.parts]
    for path in full:
        valid = _valid_from_name(path.stem)
        if valid is None or valid >= full_cutoff:
            continue
        variable = path.parent.name
        if variable in THIN_VARS:
            _copy_thin(path, thin / variable / path.name, now)
        _unlink_pair(path)
        removed.append(str(path))
    if thin.exists():
        for path in list(thin.rglob("*.f16")):
            valid = _valid_from_name(path.stem)
            if valid is not None and valid < year_cutoff:
                _unlink_pair(path)
                removed.append(str(path))
    _prune_empty(directory)
    return removed


def _copy_thin(source: Path, dest: Path, now: datetime) -> None:
    sidecar = source.with_suffix(".json")
    dest_side = dest.with_suffix(".json")
    if dest.is_file() and sidecar.is_file() and dest_side.is_file():
        # A newer run already wrote this valid time.
        if dest.stat().st_mtime >= source.stat().st_mtime:
            return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    if sidecar.is_file():
        shutil.copy2(sidecar, dest_side)


def _dated(directory: Path, cutoff: datetime, *, keep_latest: bool) -> list[str]:
    """Delete dated run files. ``{id}.json`` at the top of the directory stays."""
    if not directory.exists():
        return []
    removed = []
    for path in directory.rglob("*.json"):
        if keep_latest and path.parent == directory:
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed.append(str(path))
    _prune_empty(directory)
    return removed


def _obs(path: Path, cutoff: datetime) -> int:
    if not path.is_file():
        return 0
    stamp = cutoff.strftime("%Y%m%d%H%M%S")
    connection = sqlite3.connect(path)
    try:
        cursor = connection.execute("DELETE FROM obs WHERE aifstime_utc < ?", (stamp,))
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def _valid_from_name(stem: str) -> datetime | None:
    try:
        return datetime.strptime(stem, "%Y%m%dT%HZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _unlink_pair(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_suffix(".json").unlink(missing_ok=True)


def _prune_empty(directory: Path) -> None:
    if not directory.exists():
        return
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
