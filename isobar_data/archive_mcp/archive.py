"""Read-only queries over the collector's published grid and sidecar contract."""
from __future__ import annotations

import bisect
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from isobar_data.contract import CONTRACT, GLOBAL_GRID_FAMILY, GRID_FAMILY, accepted_schema_versions
from isobar_data.fetch_ecmwf import run_id
from isobar_data.grid import OPTIONAL, REQUIRED
from isobar_data.retain import THIN_VARS, _valid_from_name
from isobar_data.storage import read_maybe_zstd

FAMILIES = (GRID_FAMILY.removeprefix('grids/'), GLOBAL_GRID_FAMILY.removeprefix('grids/'))
UNITS = {variable: units for _, _, _, variable, units in REQUIRED + OPTIONAL}
WIND_UNITS = {'u_m_s': 'm/s', 'v_m_s': 'm/s', 'speed_kt': 'kt', 'direction_from_deg': 'degrees true, from'}
KT_PER_M_S = 3600 / 1852


def utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError('time must be an ISO-8601 string with a UTC offset')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError('time must explicitly use UTC (Z or +00:00)')
    return moment.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace('+00:00', 'Z')


def coordinate(lat, lon) -> tuple[float, float]:
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lat, lon)):
        raise ValueError('coordinates must be finite numbers')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('coordinates must be latitude [-90,90], longitude [-180,180] degrees')
    return float(lat), float(lon)


def read_json(path: Path, root: Path) -> dict:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('archive path escapes store')
    doc = json.loads(read_maybe_zstd(path))
    if not isinstance(doc, dict):
        raise ValueError('expected a JSON object')
    return doc


def family_name(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in FAMILIES:
        raise ValueError('family must be ' + ' or '.join(FAMILIES))
    return value


def wind(u: float | None, v: float | None) -> dict | None:
    if u is None or v is None:
        return None
    speed = math.hypot(u, v)
    return {'u_m_s': u, 'v_m_s': v, 'speed_kt': speed * KT_PER_M_S,
            'direction_from_deg': None if speed < 1e-12 else math.degrees(math.atan2(-u, -v)) % 360}


@dataclass
class Run:
    family: str
    id: str
    time: datetime
    hours: tuple[int, ...]
    grid: dict
    files: dict[str, dict[int, Path]] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    schedule_known: bool = True
    locations: set[str] = field(default_factory=set)

    def metadata(self) -> dict:
        return {'family': self.family, 'run_id': self.id, 'run_time_utc': iso(self.time)}

    def covers(self, lat: float, lon: float) -> bool:
        g = self.grid
        if not g:
            return self.family == FAMILIES[1]
        return g['south'] <= lat <= g['north'] and (g.get('wraps_longitude', False) or g['west'] <= lon <= g['east'])


class Sampler:
    """A bounded per-query cache; retention changes are seen on the next call."""
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[Path, tuple[dict, np.ndarray]] = {}

    def field(self, path: Path, run: Run, hour: int) -> tuple[dict, np.ndarray]:
        if path in self.cache:
            return self.cache[path]
        side = read_json(path.with_suffix('.json'), self.root)
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('archive path escapes store')
        if side.get('dtype') != 'float16' or side.get('endian') != 'little' or side.get('order') != 'north-to-south, west-to-east':
            raise ValueError('unsupported grid encoding')
        if utc(side['run']) != run.time or utc(side['valid_time']) != run.time + timedelta(hours=hour):
            raise ValueError('sidecar run/valid time does not match the manifest')
        if 'lead_hours' in side and side['lead_hours'] != hour:
            raise ValueError('sidecar lead hour does not match the manifest')
        if not isinstance(side.get('units'), str) or not side['units']:
            raise ValueError('sidecar units are missing')
        nx, ny = side['nx'], side['ny']
        if type(nx) is not int or type(ny) is not int or nx < 1 or ny < 1 or nx * ny > 20_000_000:
            raise ValueError('invalid grid dimensions')
        for key in ('lat0', 'lon0', 'dlat', 'dlon', 'fill'):
            if not isinstance(side[key], (int, float)) or not math.isfinite(side[key]):
                raise ValueError('invalid grid geometry or fill')
        if side['dlat'] >= 0 or side['dlon'] <= 0:
            raise ValueError('unsupported grid orientation')
        if side.get('wraps_longitude') and not math.isclose(nx * side['dlon'], 360, abs_tol=1e-5):
            raise ValueError('wrapped grid must cover 360 degrees')
        if path.stat().st_size != nx * ny * 2:
            raise ValueError('grid byte count does not match sidecar')
        values = np.frombuffer(read_maybe_zstd(path), dtype='<f2').reshape(ny, nx)
        if len(self.cache) >= 16:
            self.cache.pop(next(iter(self.cache)))
        self.cache[path] = side, values
        return side, values

    def spatial(self, run: Run, var: str, hour: int, lat: float, lon: float) -> tuple[float | None, str | None, str | None]:
        units = run.units.get(var, UNITS.get(var))
        path = run.files.get(var, {}).get(hour)
        if path is None:
            return None, units, f'missing {var} at listed forecast hour {hour}'
        try:
            side, values = self.field(path, run, hour)
            units = side['units']
            nx, ny = side['nx'], side['ny']
            y = (lat - side['lat0']) / side['dlat']
            wrap = side.get('wraps_longitude', False)
            x = ((lon - side['lon0']) % 360 if wrap else lon - side['lon0']) / side['dlon']
            if y < -1e-8 or y > ny - 1 + 1e-8 or (not wrap and (x < -1e-8 or x > nx - 1 + 1e-8)):
                return None, units, 'coordinate outside this grid'
            y = min(max(y, 0), ny - 1)
            x = x % nx if wrap else min(max(x, 0), nx - 1)
            x0, y0 = math.floor(x), math.floor(y)
            x1, y1 = (x0 + 1) % nx if wrap else min(x0 + 1, nx - 1), min(y0 + 1, ny - 1)
            fx, fy = x - x0, y - y0
            total = 0.0
            for row, col, weight in ((y0, x0, (1-fx)*(1-fy)), (y0, x1, fx*(1-fy)),
                                      (y1, x0, (1-fx)*fy), (y1, x1, fx*fy)):
                if weight <= 1e-14:
                    continue
                value = float(values[row, col])
                if not math.isfinite(value) or value == side['fill']:
                    return None, units, 'missing grid cell in interpolation stencil'
                total += weight * value
            return total, units, None
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            return None, units, f'unreadable grid: {exc}'

    def sample(self, run: Run | None, var: str, lat: float, lon: float, moment: datetime) -> dict:
        result = {'var': var, 'lat': lat, 'lon': lon, 'coordinate_units': 'degrees',
                  'valid_time_utc': iso(moment), 'family': None, 'run_id': None, 'run_time_utc': None,
                  'forecast_hour': None, 'forecast_hour_units': 'h', 'source_hours': [],
                  'source_valid_times_utc': [], 'value': None,
                  'units': WIND_UNITS if var == 'wind10' else UNITS.get(var), 'reason': None}
        if run is None:
            return {**result, 'reason': 'no retained run for this request'}
        lead = (moment - run.time).total_seconds() / 3600
        result.update(run.metadata(), forecast_hour=lead)
        if var == 'wind10':
            u = self.sample(run, 'u10', lat, lon, moment)
            v = self.sample(run, 'v10', lat, lon, moment)
            result.update(source_hours=sorted(set(u['source_hours']+v['source_hours'])),
                          source_valid_times_utc=sorted(set(u['source_valid_times_utc']+v['source_valid_times_utc'])))
            result['component_sources'] = {name: {key: component[key] for key in
                ('run_id','run_time_utc','valid_time_utc','source_hours','source_valid_times_utc','reason')}
                for name,component in (('u10',u),('v10',v))}
            if u['units'] != 'm/s' or v['units'] != 'm/s':
                return {**result, 'reason': 'wind components must use m/s'}
            result['value'] = wind(u['value'], v['value'])
            result['reason'] = '; '.join(dict.fromkeys(r for r in (u['reason'],v['reason']) if r)) or None
            if result['value'] and result['value']['direction_from_deg'] is None:
                result['direction_reason'] = 'calm wind has no direction'
            return result
        result['units'] = run.units.get(var, UNITS.get(var))
        if var == 'tp':
            result.update(accumulation='since model run', accumulation_start_utc=iso(run.time))
        if var not in run.files:
            return {**result, 'reason': f'variable {var!r} is not retained for this run', 'available_variables': sorted(run.files)}
        hours = run.hours
        i = bisect.bisect_left(hours, lead)
        if i < len(hours) and hours[i] == lead:
            endpoints = [hours[i]]
        elif i == 0 or i == len(hours):
            return {**result, 'reason': 'valid time outside retained forecast horizon'}
        elif not run.schedule_known:
            return {**result, 'reason': 'forecast hour schedule unavailable for thinned run; exact times only'}
        else:
            endpoints = [hours[i-1], hours[i]]
        result['source_hours'] = endpoints
        result['source_valid_times_utc'] = [iso(run.time + timedelta(hours=h)) for h in endpoints]
        values = [self.spatial(run, var, h, lat, lon) for h in endpoints]
        result['units'] = values[0][1]
        if any(v[0] is None for v in values):
            return {**result, 'reason': '; '.join(dict.fromkeys(v[2] for v in values if v[2]))}
        if any(v[1] != result['units'] for v in values):
            return {**result, 'reason': 'units differ between adjacent hours'}
        result['value'] = values[0][0]
        if len(values) == 2:
            fraction = (lead-endpoints[0]) / (endpoints[1]-endpoints[0])
            result['value'] += fraction * (values[1][0]-values[0][0])
        if var in ('u10', 'v10'):
            # The aggregate tool is the explicit vector interface; raw components stay scalar.
            result['wind_tool'] = 'sample(var="wind10")'
        return result


class ArchiveStore:
    def __init__(self, root: Path, *, now: datetime | None = None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError('store must be an existing directory')
        self.now = now

    def _catalog(self) -> tuple[list[Run], list[dict]]:
        runs: dict[tuple[str, str], Run] = {}
        issues = []
        for family in FAMILIES:
            base = self.root / 'products' / 'grids' / family
            for manifest in sorted((base / 'runs').glob('*/manifest.json')):
                try:
                    doc = read_json(manifest, self.root)
                    version = doc.get('schema_version', doc.get('schema', 1))
                    if type(version) is not int or version not in accepted_schema_versions('grids/' + family):
                        raise ValueError('unsupported grid schema')
                    if doc.get('contract', CONTRACT) != CONTRACT or doc.get('family', 'grids/'+family) != 'grids/'+family:
                        raise ValueError('unsupported grid contract/family')
                    moment = _valid_from_name(manifest.parent.name)
                    if moment is None or utc(doc['run']) != moment:
                        raise ValueError('run id does not match manifest run')
                    hours = doc.get('forecast_hours')
                    if hours is None and version == 1:
                        hours = list(range(0, 97, 3))  # contract.py: schema 1's fixed 33-frame horizon
                    if not isinstance(hours, list) or not hours or any(type(h) is not int or h < 0 for h in hours) or hours != sorted(set(hours)):
                        raise ValueError('invalid forecast_hours')
                    grid = doc['grid']
                    if not isinstance(grid, dict) or not all(isinstance(grid.get(k), (int, float)) and math.isfinite(grid[k]) for k in ('north','south','west','east')):
                        raise ValueError('invalid manifest grid bounds')
                    item = Run(family, manifest.parent.name, moment, tuple(hours), grid)
                    for path in manifest.parent.glob('*/*.f16'):
                        valid = _valid_from_name(path.stem)
                        if valid is None or not path.resolve().is_relative_to(self.root):
                            continue
                        hour = (valid-moment).total_seconds()/3600
                        if hour not in hours:
                            continue
                        var = path.parent.name
                        item.files.setdefault(var, {})[int(hour)] = path
                        item.units[var] = UNITS.get(var)
                        if item.units[var] is None:
                            try:
                                item.units[var] = read_json(path.with_suffix('.json'), self.root).get('units')
                            except (OSError, ValueError):
                                pass
                    item.locations.add('runs')
                    runs[family, item.id] = item
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    issues.append({'family': family, 'run_id': manifest.parent.name, 'reason': str(exc)})
            # retain._grids can move mslp/t850 into thin/; provenance remains in each sidecar.
            for var in sorted(THIN_VARS):
                for path in (base / 'thin' / var).glob('*.f16'):
                    try:
                        side = read_json(path.with_suffix('.json'), self.root)
                        moment, valid = utc(side['run']), utc(side['valid_time'])
                        hour = (valid-moment).total_seconds()/3600
                        if hour < 0 or not hour.is_integer() or _valid_from_name(path.stem) != valid:
                            raise ValueError('invalid thinned forecast time')
                        key = (family, run_id(moment))
                        if key not in runs:
                            runs[key] = Run(family, key[1], moment, (), {}, schedule_known=False)
                        item = runs[key]
                        if item.schedule_known and int(hour) not in item.hours:
                            continue
                        item.files.setdefault(var, {}).setdefault(int(hour), path)
                        item.units[var] = side.get('units', UNITS.get(var))
                        item.locations.add('thin')
                        if not item.schedule_known:
                            item.hours = tuple(sorted(set(item.hours) | {int(hour)}))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        issues.append({'family': family, 'path': str(path.relative_to(self.root)), 'reason': str(exc)})
        return sorted(runs.values(), key=lambda r: (r.time, r.family)), issues

    def list_runs(self) -> dict:
        runs, issues = self._catalog()
        families = {family: [] for family in FAMILIES}
        for item in runs:
            families[item.family].append({**item.metadata(), 'forecast_hours': list(item.hours),
                'valid_times_utc': [iso(item.time+timedelta(hours=h)) for h in item.hours],
                'variables': item.units, 'available_hours': {v: sorted(h) for v,h in item.files.items()},
                'schedule_known': item.schedule_known, 'locations': sorted(item.locations)})
        return {'families': families, 'forecast_hour_units': 'h', 'issues': issues}

    def _select(self, runs: list[Run], lat: float, lon: float, moment: datetime,
                run: str | None = None, family: str | None = None) -> Run | None:
        family_name(family)
        if run is not None:
            if not isinstance(run, str):
                raise ValueError('run must be a run id or family/run id')
            if '/' in run:
                parts = run.split('/')
                if len(parts) != 2 or (family and family != parts[0]):
                    raise ValueError('invalid or conflicting family/run id')
                family, run = family_name(parts[0]), parts[1]
            if _valid_from_name(run) is None:
                raise ValueError('run must use YYYYMMDDTHHZ')
        candidates = [r for r in runs if (family is None or r.family == family) and (r.id == run if run else r.time <= moment)]
        if not candidates:
            return None
        if family is None:
            regional = [r for r in candidates if r.family == FAMILIES[0] and r.covers(lat, lon)]
            candidates = regional or [r for r in candidates if r.family == FAMILIES[1]] or candidates
        covering = [r for r in candidates if r.hours and r.hours[0] <= (moment-r.time).total_seconds()/3600 <= r.hours[-1]]
        return max(covering or candidates, key=lambda r: r.time)

    def sample(self, var, lat, lon, valid_time_utc, run=None, family=None) -> dict:
        lat, lon = coordinate(lat, lon)
        moment = utc(valid_time_utc)
        if not isinstance(var, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', var):
            raise ValueError('invalid variable name')
        runs, _ = self._catalog()
        chosen = self._select(runs, lat, lon, moment, run, family)
        result = Sampler(self.root).sample(chosen, var, lat, lon, moment)
        if var in ('u10', 'v10'):
            vector = Sampler(self.root).sample(chosen, 'wind10', lat, lon, moment)
            result['wind'] = {'value': vector['value'], 'units': WIND_UNITS, 'reason': vector['reason']}
        return result

    def profile(self, lat, lon, valid_time_utc, run=None, family=None) -> dict:
        from .products import point_profiles
        lat, lon = coordinate(lat, lon)
        moment = utc(valid_time_utc)
        runs, _ = self._catalog()
        chosen = self._select(runs, lat, lon, moment, run, family)
        sampler = Sampler(self.root)
        levels = []
        retained = []
        for item in runs:
            if family is not None and item.family != family:
                continue
            variables = {}
            for name in item.files:
                match = re.fullmatch(r'(?:t|u|v|z|r|q|w)(\d{2,4})',name)
                if (name not in UNITS or name == 't850') and match and 0 < int(match[1]) <= 1100:
                    variables[name] = int(match[1])
            if variables:
                retained.append({**item.metadata(),'variables_hpa':variables})
        if chosen:
            for var in sorted(chosen.files):
                if var in UNITS and var != 't850':
                    continue
                match = re.fullmatch(r'(?:t|u|v|z|r|q|w)(\d{2,4})', var)
                if match and 0 < int(match[1]) <= 1100:
                    levels.append({'pressure': int(match[1]), 'pressure_units': 'hPa',
                                   **sampler.sample(chosen, var, lat, lon, moment)})
        return {'lat': lat, 'lon': lon, 'coordinate_units': 'degrees', 'valid_time_utc': iso(moment),
                **(chosen.metadata() if chosen else {'run_id': None, 'run_time_utc': None, 'family': None}),
                'levels': levels or None,
                'available_pressure_levels_hpa': sorted({p for r in retained for p in r['variables_hpa'].values()},reverse=True),
                'sampled_pressure_levels_hpa': sorted({v['pressure'] for v in levels},reverse=True),
                'retained_pressure_variables': retained,
                'reason': None if levels else 'no pressure-level grids in the selected run; see retained_pressure_variables and point_profiles for availability',
                'point_profiles': point_profiles(self, lat, lon, iso(moment), run=run)}

    def section(self, points, valid_time_utc, var, samples=100, png=False, family=None) -> dict:
        from .route import route_points
        if type(png) is not bool:
            raise ValueError('png must be a boolean')
        family_name(family)
        moment = utc(valid_time_utc)
        route = route_points(points, samples)
        runs, _ = self._catalog()
        # One grid family and cycle for the whole route; global if any point is outside AU.
        if family is None:
            au = self._select(runs,route[0][1],route[0][2],moment,family=FAMILIES[0])
            family = FAMILIES[0] if au and all(au.covers(lat,lon) for _,lat,lon in route) else FAMILIES[1]
        chosen = self._select(runs, route[0][1], route[0][2], moment, family=family)
        sampler = Sampler(self.root)
        values = [{**sampler.sample(chosen,var,lat,lon,moment), 'distance_km': distance} for distance,lat,lon in route]
        result = {'var': var, 'valid_time_utc': iso(moment),
                  **(chosen.metadata() if chosen else {'run_id': None,'run_time_utc': None,'family': family}),
                  'units': values[0]['units'], 'distance_units': 'km', 'samples': values,
                  'png_path': None, 'png_reason': 'matplotlib is not a project dependency' if png else 'not requested'}
        return result

    def run_change(self, var, lat, lon, valid_time_utc, family=None) -> dict:
        lat, lon = coordinate(lat, lon)
        family_name(family)
        moment = utc(valid_time_utc)
        runs, _ = self._catalog()
        sampler = Sampler(self.root)
        return {'var': var, 'lat': lat, 'lon': lon, 'coordinate_units': 'degrees',
                'valid_time_utc': iso(moment), 'units': WIND_UNITS if var == 'wind10' else UNITS.get(var),
                'runs': [sampler.sample(r,var,lat,lon,moment) for r in runs if family is None or r.family == family],
                'reason': None if any(family is None or r.family == family for r in runs) else 'no retained runs in requested family'}

    def forecast_vs_observed(self, station=None, icao=None, hours_back=48) -> dict:
        from .products import forecast_vs_observed
        return forecast_vs_observed(self, station=station, icao=icao, hours_back=hours_back)

    def facts(self, icao) -> dict:
        from .products import facts
        return facts(self, icao)
