"""Product joins use the collector's SQLite schema and published JSON formats."""
from __future__ import annotations

import json
import math
from datetime import timedelta

import pytest

from isobar_data.archive_mcp.archive import ArchiveStore, KT_PER_M_S
from isobar_data.normalise import connect_obs, insert_obs
from isobar_data.publish import publish_run
from test_archive_mcp import RUN, make_run


def point(root, *, run=RUN, temperature=20, point_id='YPPH', lat=-31.5, lon=115.5):
    family='points/ecmwf_ifs025_upper'
    generation=run.strftime('%Y-%m-%dT%H%M%SZ')
    folder=root/'products'/family/'runs'/generation
    folder.mkdir(parents=True,exist_ok=True)
    body={'schema_version':1,'contract':'isobar-data','family':family,'id':point_id,
          'latitude':lat,'longitude':lon,'run':run.isoformat().replace('+00:00','Z'),
          'native_step_hours':3,'time':['2026-10-08T00:00','2026-10-08T03:00'],
          'units':{'temperature_850hPa':'°C','wind_speed_850hPa':'kn'},
          'levels':{'850':{'temperature_c':[temperature,temperature-1],'wind_speed_kt':[10,None]},
                    '500':{'height_m':[5500,5510]}}}
    (folder/(point_id+'.json')).write_text(json.dumps(body))
    publish_run(root,family,generation,[point_id+'.json'])
    return folder/(point_id+'.json')


def add_obs(root, **overrides):
    row={'wmo':94151,'aifstime_utc':'20261008013000','product_id':'IDW60910',
         'name':'Perth Airport','lat':-31.5,'lon':115.5,'air_temp':20,'press_msl':1000,
         'wind_spd_kt':10,'wind_spd_kmh':18.52,'wind_dir_deg':210,'rain_trace':'1.2'}
    row.update(overrides)
    db=connect_obs(root/'products/obs/obs.sqlite')
    insert_obs(db,[row]); db.close()


@pytest.fixture
def store(tmp_path):
    make_run(tmp_path,values={'t2m':{0:[[10]*2]*2,3:[[22]*2]*2},
        'mslp':{0:[[1000]*2]*2,3:[[1012]*2]*2},
        'u10':{0:[[3]*2]*2,3:[[3]*2]*2}, 'v10':{0:[[4]*2]*2,3:[[4]*2]*2},
        'tp':{0:[[0]*2]*2,3:[[6]*2]*2}})
    point(tmp_path)
    add_obs(tmp_path)
    return ArchiveStore(tmp_path,now=RUN+timedelta(hours=4))


def test_profiles_use_latest_matching_cycle_and_exact_sites(store):
    # File ordering must not select this older run.
    point(store.root,run=RUN-timedelta(hours=12),temperature=-10)
    got=store.profile(-31.5,115.5,'2026-10-08T03:00:00Z')['point_profiles']
    assert got['value']['850']['temperature_c']==19
    assert got['value']['850']['wind_speed_kt'] is None
    assert got['run_id']=='20261008T00Z'
    assert got['units']['temperature_c']=='degC'
    older=store.profile(-31.5,115.5,'2026-10-08T03:00:00Z',run='20261007T12Z')['point_profiles']
    assert older['value']['850']['temperature_c']==-11
    missing=store.profile(-31,115,'2026-10-08T03:00:00Z')['point_profiles']
    assert missing['value'] is None and 'not gridded' in missing['reason']
    assert missing['available_sites'][0]['pressure_levels_hpa']==[850,500]
    mid=store.profile(-31.5,115.5,'2026-10-08T01:30:00Z')['point_profiles']
    assert mid['value'] is None and 'exact valid time' in mid['reason']


def test_facts_use_published_generation_and_preserve_nulls(store):
    root=store.root
    family=root/'products/aviation'
    directory=family/'runs/generation1'; directory.mkdir(parents=True)
    doc={'licence_id':'ourairports-public-domain','runways':[{'icao':'YPPH','length_ft':10000,
        'le_heading_degT':14,'le_displaced_threshold_ft':None,'closed':False}]}
    (directory/'runways.json').write_text(json.dumps(doc))
    (family/'runways.json').write_text(json.dumps({'runways':[{'icao':'YPPH','length_ft':1}]}))
    publish_run(root,'aviation','generation1',['runways.json'])
    got=store.facts('ypph')
    assert got['runways'][0]['length_ft']==10000
    assert got['runways'][0]['le_displaced_threshold_ft'] is None
    assert got['units']['length_ft']=='ft' and got['run_id']=='generation1'
    assert got['aerodrome'] is None and got['aerodrome_reason']
    assert got['valid_time_utc'] is None and got['time_reason']
    missing=store.facts('YZZZ')
    assert missing['runways'] is None and missing['reason']


def test_forecast_errors_run_metadata_and_incomparable_precip(store):
    got=store.forecast_vs_observed(station='94151',hours_back=48)
    assert got['station']['wmo']==94151
    row=got['observations'][0]
    assert row['run_id']=='20261008T00Z' and row['valid_time_utc']=='2026-10-08T01:30:00Z'
    assert row['source_valid_times_utc']==['2026-10-08T00:00:00Z','2026-10-08T03:00:00Z']
    values=row['values']
    assert values['t2m']=={'forecast':16,'observed':20,'error':-4,'units':'degC','reason':None}
    assert values['mslp']['error']==6
    assert values['speed_kt']['error']==pytest.approx(5*KT_PER_M_S-10)
    assert values['direction_from_deg']['error']==pytest.approx(6.86989764584402)
    assert values['u_m_s']['observed']==pytest.approx(5/KT_PER_M_S)
    assert values['v_m_s']['observed']==pytest.approx(10/KT_PER_M_S*math.sqrt(3)/2)
    assert values['tp']['forecast']==3 and values['tp']['observed']==1.2
    assert values['tp']['error'] is None and 'incomparable' in values['tp']['reason']
    assert store.forecast_vs_observed(station='Perth Airport')['observations']==got['observations']


def test_icao_uses_published_point_and_reports_nearest_match(store):
    add_obs(store.root,wmo=94608,name='Perth',lat=-31.6,lon=115.6)
    got=store.forecast_vs_observed(icao='YPPH')
    assert got['station']['wmo']==94151
    assert 'icao to wmo' in got['station']['resolution'].lower()
    assert len(got['station_candidates'])==2
    assert got['station_candidates'][0]['distance_km']==pytest.approx(0)
    add_obs(store.root,wmo=99999,name='Same location')
    tied=store.forecast_vs_observed(icao='YPPH')
    assert tied['station']['wmo']==94151
    assert 'ambiguous' not in (tied.get('reason') or '')


def _north(lat, kilometres):
    return lat + kilometres / 6371.0088 * (180 / math.pi)


def test_ypph_near_tie_prefers_perth_airport_over_gooseberry_hill(tmp_path):
    # Gooseberry Hill is 0.09 km closer than Perth Airport, inside the old 0.1 km tie.
    make_run(tmp_path,values={'t2m':{0:[[10]*2]*2}})
    point(tmp_path)
    add_obs(tmp_path,wmo=95640,name='Gooseberry Hill',lat=_north(-31.5,8.08),lon=115.5)
    add_obs(tmp_path,wmo=94151,name='Perth Airport',lat=_north(-31.5,8.17),lon=115.5)
    got=ArchiveStore(tmp_path,now=RUN+timedelta(hours=4)).forecast_vs_observed(icao='YPPH')
    assert got['station_candidates'][0]['name']=='Gooseberry Hill'
    assert got['station']['wmo']==94151
    assert got['station']['name']=='Perth Airport'
    assert 'ambiguous' not in (got.get('reason') or '')


def test_ypph_mapping_selects_the_bureau_station_when_its_name_is_not_airport(tmp_path):
    make_run(tmp_path,values={'t2m':{0:[[10]*2]*2}})
    point(tmp_path)
    add_obs(tmp_path,wmo=95640,name='Gooseberry Hill',lat=_north(-31.5,8.08),lon=115.5)
    add_obs(tmp_path,wmo=94151,name='Perth',lat=_north(-31.5,8.17),lon=115.5)
    got=ArchiveStore(tmp_path,now=RUN+timedelta(hours=4)).forecast_vs_observed(icao='YPPH')
    assert got['station']['wmo']==94151
    assert 'icao to wmo' in got['station']['resolution'].lower()


def test_unmapped_icao_tie_uses_place_name_from_runway_facts(tmp_path):
    make_run(tmp_path,values={'t2m':{0:[[10]*2]*2}})
    point(tmp_path,point_id='YMML',lat=-37.67,lon=144.83)
    family=tmp_path/'products/aviation'
    directory=family/'runs/generation1'
    directory.mkdir(parents=True)
    (directory/'runways.json').write_text(json.dumps({
        'licence_id':'ourairports-public-domain',
        'runways':[{'icao':'YMML','name':'Tullamarine','length_ft':11998}],
    }))
    publish_run(tmp_path,'aviation','generation1',['runways.json'])
    add_obs(tmp_path,wmo=94865,name='Essendon',lat=_north(-37.67,8.08),lon=144.83)
    add_obs(tmp_path,wmo=94866,name='Tullamarine Aws',lat=_north(-37.67,8.17),lon=144.83)
    got=ArchiveStore(tmp_path,now=RUN+timedelta(hours=4)).forecast_vs_observed(icao='YMML')
    assert got['station']['wmo']==94866
    assert 'ambiguous' not in (got.get('reason') or '')


def test_unmapped_icao_tie_prefers_the_station_named_as_an_airport(tmp_path):
    make_run(tmp_path,values={'t2m':{0:[[10]*2]*2}})
    point(tmp_path,point_id='YBBN',lat=-27.38,lon=153.12)
    add_obs(tmp_path,wmo=94576,name='Archerfield',lat=_north(-27.38,8.08),lon=153.12)
    add_obs(tmp_path,wmo=94578,name='Brisbane Airport',lat=_north(-27.38,8.17),lon=153.12)
    got=ArchiveStore(tmp_path,now=RUN+timedelta(hours=4)).forecast_vs_observed(icao='YBBN')
    assert got['station']['wmo']==94578
    assert 'aerodrome' in got['station']['resolution'].lower()


def test_unmapped_icao_tie_stays_ambiguous_without_a_name(tmp_path):
    make_run(tmp_path,values={'t2m':{0:[[10]*2]*2}})
    point(tmp_path,point_id='YMML',lat=-37.67,lon=144.83)
    add_obs(tmp_path,wmo=94865,name='North Hill',lat=_north(-37.67,8.08),lon=144.83)
    add_obs(tmp_path,wmo=94866,name='South Hill',lat=_north(-37.67,8.17),lon=144.83)
    got=ArchiveStore(tmp_path,now=RUN+timedelta(hours=4)).forecast_vs_observed(icao='YMML')
    assert 'ambiguous' in got['reason']


def test_missing_observations_no_future_runs_and_window(store):
    # A future cycle must not be used to verify an earlier observation.
    make_run(store.root,run=RUN+timedelta(hours=12),values={'t2m':{0:[[99]*2]*2}})
    add_obs(store.root,aifstime_utc='20261008020000',air_temp=None,wind_spd_kt=None,wind_spd_kmh=None,wind_dir_deg=None)
    add_obs(store.root,aifstime_utc='20261001000000')
    got=store.forecast_vs_observed(station='94151',hours_back=3)
    assert len(got['observations'])==2
    assert all(row['run_id']=='20261008T00Z' for row in got['observations'])
    row=got['observations'][1]
    assert row['values']['t2m']['error'] is None and row['values']['t2m']['reason']=='observation missing'
    assert row['values']['speed_kt']['observed'] is None
    assert row['values']['u_m_s']['error'] is None
    assert got['window']['start_utc']=='2026-10-08T01:00:00Z'


def test_wind_direction_error_wraps_and_kmh_fallback(store):
    from isobar_data.archive_mcp.products import _comparison
    assert _comparison(10,350,'degrees',circular=True)['error']==20
    assert _comparison(350,10,'degrees',circular=True)['error']==-20
    add_obs(store.root,aifstime_utc='20261008020000',wind_spd_kt=None,wind_spd_kmh=18.52)
    values=store.forecast_vs_observed(station='94151')['observations'][1]['values']
    assert values['speed_kt']['observed']==10


def test_missing_grid_hour_propagates_to_error(store):
    p=store.root/'products/grids/ecmwf_ifs025/runs/20261008T00Z/t2m/20261008T03Z.f16'
    p.unlink()
    got=store.forecast_vs_observed(station='94151')['observations'][0]['values']['t2m']
    assert got['forecast'] is None and got['error'] is None
    assert 'missing t2m at listed forecast hour 3' in got['reason']


def test_obs_queries_do_not_change_database_or_create_files(store):
    def snapshot():
        return {p.relative_to(store.root):(p.read_bytes(),p.stat().st_mtime_ns) for p in store.root.rglob('*') if p.is_file()}
    before=snapshot()
    store.forecast_vs_observed(station='94151')
    assert snapshot()==before


def test_missing_products_corrupt_db_and_invalid_requests(tmp_path):
    store=ArchiveStore(tmp_path,now=RUN)
    assert store.facts('YPPH')['runways'] is None
    assert store.forecast_vs_observed(station='94151')['reason']
    assert store.forecast_vs_observed(icao='YPPH')['reason']
    path=tmp_path/'products/obs/obs.sqlite'; path.parent.mkdir(parents=True); path.write_bytes(b'not sqlite')
    assert 'unreadable' in store.forecast_vs_observed(station='94151')['reason']
    for params in ({},{'station':'94151','icao':'YPPH'},{'station':'94151','hours_back':0}):
        with pytest.raises(ValueError):
            store.forecast_vs_observed(**params)
    with pytest.raises(ValueError):
        store.facts('../../other')


def test_live_wal_rows_are_seen_without_touching_store(store):
    # Leave committed rows only in a live WAL: immutable=1 would silently miss them.
    db=connect_obs(store.root/'products/obs/obs.sqlite')
    db.execute('INSERT INTO obs (wmo,aifstime_utc,product_id,name,lat,lon,air_temp) VALUES (94151,?,?,?,?,?,?)',
               ('20261008030000','IDW60910','Perth Airport',-31.5,115.5,23))
    db.commit()
    paths=list((store.root/'products/obs').iterdir())
    before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in paths}
    try:
        rows=store.forecast_vs_observed(station='94151')['observations']
        assert len(rows)==2
        assert rows[-1]['values']['t2m']['observed']==23
        assert {p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in paths}==before
    finally:
        db.close()


def test_busy_snapshot_reports_unavailable(store,monkeypatch):
    from isobar_data.archive_mcp import observations
    original=observations._signature
    count=0
    def changing(path):
        nonlocal count
        count+=1
        value=original(path)
        return (*value,count) if value else None
    monkeypatch.setattr(observations,'_signature',changing)
    result=store.forecast_vs_observed(station='94151')
    assert not result['observations']
    assert 'changed during snapshot' in result['reason']
