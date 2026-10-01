"""Load the on-disk thresholds and the station list."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "isobar.toml"

SHORE_NAMES = {"onshore", "cross-on", "cross-off", "offshore"}


class ConfigError(ValueError):
    """The config asks for more than the budgeted cap, or a bad threshold."""


@dataclass(frozen=True)
class SurfacePoint:
    id: str
    latitude: float
    longitude: float
    onshore_from_deg: float | None = None
    osm_way: int | None = None
    marine: bool = False


@dataclass(frozen=True)
class Aerodrome:
    icao: str
    latitude: float
    longitude: float
    surface_id: str


@dataclass(frozen=True)
class EnsemblePoint:
    id: str
    wmo: int
    latitude: float
    longitude: float


@dataclass(frozen=True)
class KiteConfig:
    speed_min_kt: float
    speed_max_kt: float
    excluded_shore: tuple[str, ...]
    daylight_required: bool
    gust_max_kt: float | None
    sea_breeze_min: float
    sea_breeze_max: float
    sea_breeze_above_kt: float
    inland_point: str
    timezone: str
    check_wmo: tuple[int, ...]
    airport_wmo: int


@dataclass(frozen=True)
class Config:
    kite: KiteConfig
    surface: tuple[SurfacePoint, ...]
    aerodromes: tuple[Aerodrome, ...]
    ensemble: tuple[EnsemblePoint, ...]
    coastal_wmo: tuple[int, ...]
    bom_obs: tuple[str, ...]
    bom_charts: tuple[str, ...]

    @property
    def spots(self) -> tuple[SurfacePoint, ...]:
        return tuple(point for point in self.surface if point.onshore_from_deg is not None)

    @property
    def marine_points(self) -> tuple[SurfacePoint, ...]:
        return tuple(point for point in self.surface if point.marine)


def load_config(path: Path | None = None) -> Config:
    path = path or REPO_CONFIG
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    limits = raw.get("limits") or {}
    surface = tuple(_surface(row) for row in raw.get("surface") or [])
    aerodromes = tuple(_aerodrome(row) for row in raw.get("aerodromes") or [])
    ensemble = tuple(_ensemble(row) for row in raw.get("ensemble") or [])
    marine = [point for point in surface if point.marine]
    _cap("surface points", len(surface), int(limits.get("surface_points", 8)))
    _cap("aerodromes", len(aerodromes), int(limits.get("aerodromes", 8)))
    _cap("marine points", len(marine), int(limits.get("marine_points", 4)))
    _cap("ensemble points", len(ensemble), int(limits.get("ensemble_points", 2)))
    kite = _kite(raw.get("kite") or {})
    inland = {point.id for point in surface}
    if kite.inland_point not in inland:
        raise ConfigError(f"sea-breeze inland point {kite.inland_point} is not a surface point")
    bom = raw.get("bom") or {}
    coastal = tuple(int(item) for item in (raw.get("coastal") or {}).get("wmo") or [])
    return Config(
        kite=kite,
        surface=surface,
        aerodromes=aerodromes,
        ensemble=ensemble,
        coastal_wmo=coastal,
        bom_obs=tuple(bom.get("obs") or ("IDW60910.tgz", "IDN60910.tgz")),
        bom_charts=tuple(bom.get("charts") or ("IDG00073.pdf", "IDG00074.gif")),
    )


def _cap(name: str, count: int, limit: int) -> None:
    if count > limit:
        raise ConfigError(f"{count} {name} exceeds the cap of {limit}")


def _surface(row: dict) -> SurfacePoint:
    onshore = row.get("onshore_from_deg")
    return SurfacePoint(
        id=str(row["id"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        onshore_from_deg=None if onshore is None else float(onshore),
        osm_way=None if row.get("osm_way") is None else int(row["osm_way"]),
        marine=bool(row.get("marine", False)),
    )


def _aerodrome(row: dict) -> Aerodrome:
    return Aerodrome(
        icao=str(row["icao"]).upper(),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        surface_id=str(row["surface_id"]),
    )


def _ensemble(row: dict) -> EnsemblePoint:
    return EnsemblePoint(
        id=str(row["id"]),
        wmo=int(row["wmo"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
    )


def _kite(raw: dict) -> KiteConfig:
    excluded = tuple(raw.get("excluded_shore") or ["offshore"])
    unknown = [name for name in excluded if name not in SHORE_NAMES]
    if unknown:
        raise ConfigError(f"unknown shore class {unknown[0]}")
    breeze = raw.get("sea_breeze") or {}
    low = float(raw.get("speed_min_kt", 15))
    high = float(raw.get("speed_max_kt", 30))
    if low > high:
        raise ConfigError("kite speed band is reversed")
    gust = raw.get("gust_max_kt")
    return KiteConfig(
        speed_min_kt=low,
        speed_max_kt=high,
        excluded_shore=excluded,
        daylight_required=bool(raw.get("daylight_required", True)),
        gust_max_kt=None if gust is None else float(gust),
        sea_breeze_min=float(breeze.get("direction_min_deg", 200)),
        sea_breeze_max=float(breeze.get("direction_max_deg", 250)),
        sea_breeze_above_kt=float(breeze.get("speed_above_inland_kt", 5)),
        inland_point=str(breeze.get("inland_point", "perth-airport")),
        timezone=str(breeze.get("timezone", "Australia/Perth")),
        check_wmo=tuple(int(item) for item in breeze.get("check_wmo") or (94602, 94614, 95605)),
        airport_wmo=int(breeze.get("airport_wmo", 94151)),
    )
