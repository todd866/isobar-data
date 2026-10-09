"""Point-only upper air, published runway facts, and read-only observation joins."""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from isobar_data.contract import CONTRACT
from isobar_data.fetch_ecmwf import run_id
from isobar_data.publish import published_path

from .archive import KT_PER_M_S, WIND_UNITS, Sampler, coordinate, iso, read_json, utc
from .observations import observation_snapshot

UPPER_FAMILY = 'points/ecmwf_ifs025_upper'
LEVEL_UNITS = {'temperature_c': 'degC', 'height_m': 'm', 'wind_speed_kt': 'kt',
               'wind_direction_deg': 'degrees true, from', 'relative_humidity_pct': '%',
               'cloud_cover_pct': '%', 'vertical_velocity_ms': 'm/s'}


def _icao(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z]{4}', value):
        raise ValueError('icao must be four letters')
    return value.upper()


def _document(root: Path, path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        doc = read_json(path, root)
        if doc.get('contract', CONTRACT) != CONTRACT or doc.get('schema_version', 1) != 1:
            return None
        return doc
    except (OSError, ValueError, TypeError):
        return None


def _point_time(value: str) -> datetime:
    # openmeteo.py stores hourly time in GMT without an offset, by its contract.
    moment = datetime.fromisoformat(value.replace('Z','+00:00'))
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def point_profiles(store, lat, lon, valid_time, run=None) -> dict:
    """Exact-site, exact-time upper profiles; expose other sites without substituting them."""
    requested = utc(valid_time)
    directory = store.root/'products'/UPPER_FAMILY
    documents = []
    available = {}
    requested_run = run.split('/')[-1] if run else None
    for path in sorted(directory.glob('runs/*/*.json')) + sorted(directory.glob('*.json')):
        body = _document(store.root, path)
        if not body or not isinstance(body.get('levels'), dict):
            continue
        try:
            site_lat, site_lon = coordinate(body['latitude'], body['longitude'])
            run_time = utc(body['run'])
            levels = sorted((int(k) for k in body['levels']), reverse=True)
        except (KeyError, ValueError, TypeError):
            continue
        site_id = body.get('id',path.stem)
        key = (site_id, site_lat, site_lon)
        if key not in available or available[key]['run_time_utc'] < iso(run_time):
            available[key] = {'id': site_id, 'lat': site_lat, 'lon': site_lon,
                              'pressure_levels_hpa': levels, 'run_id': run_id(run_time),
                              'run_time_utc': iso(run_time)}
        if abs(site_lat-lat) < 1e-6 and abs((site_lon-lon+180)%360-180) < 1e-6:
            if (requested_run is None and run_time <= requested) or requested_run == run_id(run_time):
                documents.append((run_time, body))
    result = {'value': None, 'units': LEVEL_UNITS, 'pressure_units': 'hPa',
              'coordinate_units': 'degrees', 'family': UPPER_FAMILY,
              'run_id': None, 'run_time_utc': None, 'valid_time_utc': iso(requested),
              'available_sites': list(available.values()), 'reason': None}
    if not documents:
        return {**result, 'reason': 'no matching upper-air site/run at coordinate; profiles are point-only, not gridded'}
    run_time, body = max(documents, key=lambda item: item[0])
    result.update(run_id=run_id(run_time),run_time_utc=iso(run_time),site_id=body.get('id'))
    try:
        times = [_point_time(t) for t in body.get('time', [])]
        index = times.index(requested)
    except (ValueError, TypeError):
        return {**result, 'reason': 'exact valid time is unavailable at this point; no time interpolation of point profiles'}
    result['value'] = {level: {key: _number(series[index]) if isinstance(series,list) and index < len(series) else None
                             for key,series in fields.items() if key in LEVEL_UNITS}
                       for level,fields in body['levels'].items() if isinstance(fields,dict)}
    result['missing_value_reason'] = 'null level values were missing in the stored point product'
    return result


def facts(store, icao) -> dict:
    icao = _icao(icao)
    path = published_path(store.root, 'aviation', 'runways.json')
    body = _document(store.root, path)
    rows = [row for row in (body or {}).get('runways', []) if isinstance(row,dict) and row.get('icao') == icao]
    return {'icao': icao, 'aerodrome': None, 'aerodrome_reason': 'aerodrome facts backend is not present in this store',
            'runways': rows or None, 'reason': None if rows else 'no retained runway facts for this ICAO',
            'units': {'length_ft':'ft', 'le_displaced_threshold_ft':'ft','he_displaced_threshold_ft':'ft',
                      'le_heading_degT':'degrees true','he_heading_degT':'degrees true'},
            'run_id': path.parent.name if path and path.parent.parent.name == 'runs' else None,
            'run_time_utc': None, 'valid_time_utc': None,
            'time_reason': 'runway product has no survey/valid timestamp; run_id is a publication generation, not a forecast',
            'licence_id': (body or {}).get('licence_id'),
            'source': path.relative_to(store.root).as_posix() if path and path.resolve().is_relative_to(store.root) else None}


# Observation stations for the aerodromes this store is built to cover.
# Perth Airport 94151 and Sydney Airport 94767 are the Bureau ids in docs/sources.md.
ICAO_WMO = {'YPPH': 94151, 'YSSY': 94767}
_AERO_NAME = re.compile(r'\b(?:airport|aero\w*)\b', re.I)
_NEAREST = 'nearest observation station within 25 km of archived ICAO model point (not an ICAO/WMO identity mapping)'


def _place_names(store, icao: str) -> list[str]:
    names = []
    for family_name, key in (('runways.json', 'runways'), ('aerodromes.json', 'aerodromes')):
        body = _document(store.root, published_path(store.root, 'aviation', family_name)) or {}
        rows = body.get(key) or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if not isinstance(row, dict) or str(row.get('icao') or '').upper() != icao:
                continue
            for field in ('name', 'aerodrome', 'municipality', 'city'):
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
    return names


def _wmo_id(row) -> int | None:
    try:
        return int(row['wmo'])
    except (TypeError, ValueError, KeyError):
        return None


def _place_hit(name: str, places: list[str]) -> bool:
    lowered = (name or '').casefold()
    return any(len(place) >= 4 and place.casefold() in lowered for place in places)


def _choose_icao_station(icao: str, ranked: list, places: list[str]):
    """Pick one station from (distance_km, row) pairs inside 25 km, nearest first."""
    if not ranked:
        return [], _NEAREST
    mapped = ICAO_WMO.get(icao)
    if mapped is not None:
        hits = [row for _distance, row in ranked if _wmo_id(row) == mapped]
        if len(hits) == 1:
            return hits, 'ICAO to WMO mapping for an aerodrome this store covers'
    nearest = ranked[0][0]
    band = [row for distance, row in ranked if distance <= nearest + 0.1]
    named = [row for row in band if _place_hit(row['name'] or '', places)]
    if len(named) == 1:
        return named, 'station name matches a place name in the archived aerodrome facts'
    aero = [row for row in band if _AERO_NAME.search(row['name'] or '')]
    if len(aero) == 1:
        return aero, 'station name matches the aerodrome'
    return band, _NEAREST


def _distance_km(a_lat, a_lon, b_lat, b_lon):
    a, b = math.radians(a_lat), math.radians(b_lat)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(b_lon-a_lon)/2)**2
    return 12742.0176*math.asin(math.sqrt(min(1,max(0,h))))


def _comparison(forecast, observed, units, reason=None, circular=False):
    error = None
    if forecast is None or observed is None:
        reason = reason or ('observation missing' if observed is None else 'forecast missing')
    elif reason is None:
        error = (forecast-observed+180)%360-180 if circular else forecast-observed
    return {'forecast': forecast, 'observed': observed, 'error': error, 'units': units, 'reason': reason}


def forecast_vs_observed(store, station=None, icao=None, hours_back=48) -> dict:
    if (station is None) == (icao is None):
        raise ValueError('supply exactly one of station or icao')
    if type(hours_back) is not int or not 1 <= hours_back <= 720:
        raise ValueError('hours_back must be an integer from 1 through 720')
    if station is not None and (not isinstance(station,(str,int)) or isinstance(station,bool) or not str(station).strip()):
        raise ValueError('station must be a WMO id or exact station name')
    end = store.now or datetime.now(timezone.utc)
    end = end.astimezone(timezone.utc)
    start = end-timedelta(hours=hours_back)
    result = {'station': None, 'icao': _icao(icao) if icao else None, 'observations': [],
              'window': {'start_utc':iso(start),'end_utc':iso(end),'hours_back':hours_back,'units':'h'},
              'error_convention': 'forecast minus observed; direction wrapped to [-180,180) degrees',
              'forecast_policy': 'latest retained covering run at or before each observation', 'reason': None}
    target = None
    if icao:
        point = _document(store.root, published_path(store.root, UPPER_FAMILY, _icao(icao)+'.json'))
        if point:
            try:
                target = coordinate(point['latitude'],point['longitude'])
            except (ValueError,TypeError,KeyError):
                pass
        if target is None:
            return {**result, 'reason': 'no archived ICAO point coordinates; supply the observation station WMO/name'}
    path = store.root/'products/obs/obs.sqlite'
    if not path.is_file() or not path.resolve().is_relative_to(store.root):
        return {**result,'reason':'observations database is unavailable inside the store'}
    bounds = (start.strftime('%Y%m%d%H%M%S'),end.strftime('%Y%m%d%H%M%S'))
    try:
        with observation_snapshot(path) as db:
            if station is not None:
                text = str(station).strip()
                condition = 'wmo = ?' if text.isdigit() else 'lower(name) = lower(?)'
                candidates = db.execute(f'SELECT wmo,name,lat,lon,MAX(aifstime_utc) AS latest FROM obs WHERE aifstime_utc BETWEEN ? AND ? AND {condition} GROUP BY wmo',(*bounds,text)).fetchall()
                method = 'WMO id' if text.isdigit() else 'exact station name'
            else:
                candidates = db.execute('SELECT wmo,name,lat,lon,MAX(aifstime_utc) AS latest FROM obs WHERE aifstime_utc BETWEEN ? AND ? GROUP BY wmo',bounds).fetchall()
                ranked = []
                for row in candidates:
                    try:
                        lat,lon = coordinate(row['lat'],row['lon'])
                    except ValueError:
                        continue
                    distance = _distance_km(*target,lat,lon)
                    if distance <= 25:
                        ranked.append((distance,row))
                ranked.sort(key=lambda item:item[0])
                result['station_candidates'] = [{'wmo':r['wmo'],'name':r['name'],'distance_km':d} for d,r in ranked]
                candidates, method = _choose_icao_station(result['icao'], ranked, _place_names(store, result['icao']))
            if len(candidates) != 1:
                return {**result,'reason':'station not found in time window' if not candidates else 'station match is ambiguous; supply WMO id'}
            station_row = candidates[0]
            result['station'] = {k:station_row[k] for k in ('wmo','name','lat','lon')}
            result['station'].update(coordinate_units='degrees',resolution=method)
            rows = db.execute('SELECT * FROM obs WHERE wmo=? AND aifstime_utc BETWEEN ? AND ? ORDER BY aifstime_utc LIMIT 5001',(station_row['wmo'],*bounds)).fetchall()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {**result,'reason':f'observations database unreadable: {exc}'}
    result['truncated'] = len(rows)>5000
    runs, _ = store._catalog()
    sampler = Sampler(store.root)
    for row in rows[:5000]:
        try:
            moment = datetime.strptime(row['aifstime_utc'],'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except (ValueError,TypeError):
            continue
        try:
            lat,lon = coordinate(row['lat'],row['lon'])
            selected = store._select(runs,lat,lon,moment)
            samples = {var:sampler.sample(selected,var,lat,lon,moment) for var in ('t2m','mslp','wind10','tp')}
        except ValueError:
            lat,lon = None,None
            samples = {var:{'value':None,'reason':'observation coordinates are missing/invalid', 'run_id':None,'run_time_utc':None,'family':None,'source_valid_times_utc':[]} for var in ('t2m','mslp','wind10','tp')}
        values = {}
        for var,column,units in (('t2m','air_temp','degC'),('mslp','press_msl','hPa')):
            s = samples[var]
            reason = s['reason'] or ('forecast units do not match observation' if s.get('units') != units and s['value'] is not None else None)
            values[var] = _comparison(s['value'],_number(row[column]),units,reason)
        speed = _number(row['wind_spd_kt'])
        if speed is None:
            kmh = _number(row['wind_spd_kmh'])
            speed = None if kmh is None else kmh/1.852
        direction = _number(row['wind_dir_deg'])
        if direction is not None and not 0 <= direction <= 360:
            direction = None
        if speed is not None and speed < 0:
            speed = None
        if speed == 0:
            direction = None
            obs_u,obs_v = 0.0,0.0
        elif speed is not None and direction is not None:
            obs_u = -speed/KT_PER_M_S*math.sin(math.radians(direction))
            obs_v = -speed/KT_PER_M_S*math.cos(math.radians(direction))
        else:
            obs_u,obs_v = None,None
        s = samples['wind10']
        vector = s['value'] or {}
        for key,observed in (('speed_kt',speed),('direction_from_deg',direction),('u_m_s',obs_u),('v_m_s',obs_v)):
            values[key] = _comparison(vector.get(key),observed,WIND_UNITS[key],s['reason'],circular=key=='direction_from_deg')
        s = samples['tp']
        values['tp'] = _comparison(s['value'],_number(row['rain_trace']),'mm',
                                  'incomparable accumulation windows: model since run; observation rain_trace since local 09:00')
        values['tp']['observed_raw'] = row['rain_trace']
        values['tp']['accumulation_start_utc'] = s.get('accumulation_start_utc')
        main = samples['t2m']
        result['observations'].append({'valid_time_utc':iso(moment), 'lat':lat,'lon':lon,'coordinate_units':'degrees',
            'wmo':row['wmo'],'product_id':row['product_id'],
            **{k:main.get(k) for k in ('run_id','run_time_utc','family','forecast_hour','source_valid_times_utc')},
            'values':values})
    if not result['observations']:
        result['reason'] = 'no observations with valid UTC timestamps in window'
    return result
