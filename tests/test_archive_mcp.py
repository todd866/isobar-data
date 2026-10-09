"""Small real-format stores and hand-computed interpolation expectations."""
from __future__ import annotations

import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from isobar_data.archive_mcp.archive import ArchiveStore, KT_PER_M_S
from isobar_data.archive_mcp.route import route_points
from isobar_data.contract import GRID_FAMILY, GLOBAL_GRID_FAMILY
from isobar_data.fetch_ecmwf import run_id
from isobar_data.grid import _run_manifest, sidecar
from isobar_data.normalise import pack_f16

FIXTURE = Path(__file__).parent / 'fixtures' / 'archive_mcp'
RUN = datetime(2026, 10, 8, tzinfo=timezone.utc)
VALID = '2026-10-08T01:30:00Z'
FAMILY = 'ecmwf_ifs025'


def make_run(root, *, run=RUN, family=GRID_FAMILY, hours=(0,3,6), values=None, wrap=False):
    directory = root/'products'/family/'runs'/run_id(run)
    directory.mkdir(parents=True, exist_ok=True)
    doc = _run_manifest(run,family)
    nx = 4 if wrap else 2
    doc.update(forecast_hours=list(hours),horizon_hours=hours[-1])
    doc['grid'].update(west=-180 if wrap else 115, east=90 if wrap else 116,
                       north=-31,south=-32,step=90 if wrap else 1,nx=nx,ny=2,wraps_longitude=wrap)
    (directory/'manifest.json').write_text(json.dumps(doc))
    for var, per_hour in (values or {}).items():
        for hour, array in per_hour.items():
            units={'mslp':'hPa','t2m':'degC','t850':'degC','u10':'m/s','v10':'m/s','tp':'mm'}.get(var,'1')
            valid=run+timedelta(hours=hour)
            side=sidecar(run,valid,units,{'t850':'t'}.get(var,var),family)
            side.update(lat0=-31,lon0=-180 if wrap else 115,dlat=-1,dlon=90 if wrap else 1,
                        nx=nx,ny=2,wraps_longitude=wrap)
            d=directory/var
            d.mkdir(exist_ok=True)
            (d/(run_id(valid)+'.json')).write_text(json.dumps(side))
            (d/(run_id(valid)+'.f16')).write_bytes(pack_f16(array))
    return directory


@pytest.fixture
def store(tmp_path):
    shutil.copytree(FIXTURE,tmp_path,dirs_exist_ok=True)
    return ArchiveStore(tmp_path)


@pytest.fixture
def computed(tmp_path):
    make_run(tmp_path,values={
        'mslp':{0:[[1000,1004],[1008,1012]],3:[[1012,1016],[1020,1024]],6:[[1024,1028],[1032,1036]]},
        't2m':{0:[[10,14],[18,22]],3:[[22,26],[30,34]]},
        'u10':{0:[[3,3],[3,3]],3:[[3,3],[3,3]]},
        'v10':{0:[[4,4],[4,4]],3:[[4,4],[4,4]]},
        'tp':{0:[[0,0],[0,0]],3:[[6,6],[6,6]]},
        't850':{0:[[-8,-4],[0,4]],3:[[4,8],[12,16]]},
    })
    return ArchiveStore(tmp_path)


def test_real_subset_read_and_run_inventory(store):
    inventory=store.list_runs()
    assert set(inventory['families'])=={'ecmwf_ifs025','ecmwf_ifs_global'}
    run=inventory['families'][FAMILY][0]
    assert run['run_id']=='20261008T00Z'
    assert run['forecast_hours']==[0,3]
    assert run['available_hours']['mslp']==[0,3]
    assert run['variables']=={'mslp':'hPa','t850':'degC'}
    assert run['valid_times_utc']==['2026-10-08T00:00:00Z','2026-10-08T03:00:00Z']
    raw=np.fromfile(FIXTURE/'products/grids/ecmwf_ifs025/runs/20261008T00Z/mslp/20261008T00Z.f16',dtype='<f2')
    got=store.sample('mslp',-31.625,115.625,'2026-10-08T00:00:00Z')
    assert got['value']==sum(float(x) for x in raw)/4


def test_bilinear_space_and_linear_adjacent_hours(computed):
    # At x=.25,y=.5 hour 0: 1000*.375+1004*.125+1008*.375+1012*.125=1005.
    # Hour 3 = 1017; halfway = 1011.
    got=computed.sample('mslp',-31.5,115.25,VALID)
    assert got['value']==1011
    assert got['units']=='hPa'
    assert got['source_hours']==[0,3]
    assert got['forecast_hour']==1.5
    assert got['run_id']=='20261008T00Z'
    assert computed.sample('t2m',-31.5,115.25,VALID)['value']==21
    rain=computed.sample('tp',-31.5,115.25,VALID)
    assert rain['value']==3
    assert rain['units']=='mm'
    assert rain['accumulation_start_utc']=='2026-10-08T00:00:00Z'


def test_wind_interpolates_components_before_speed_and_direction(computed):
    got=computed.sample('wind10',-31.5,115.5,VALID)
    assert got['value']['speed_kt']==pytest.approx(5*KT_PER_M_S)
    assert got['value']['direction_from_deg']==pytest.approx(216.86989764584402)
    assert got['units']['direction_from_deg']=='degrees true, from'
    assert computed.sample('u10',-31.5,115.5,VALID)['wind']['value']==got['value']
    path=computed.root/'products/grids/ecmwf_ifs025/runs/20261008T00Z/u10/20261008T03Z.f16'
    path.write_bytes(pack_f16([[-3,-3],[-3,-3]]))
    path=path.parent.parent/'v10/20261008T03Z.f16'
    path.write_bytes(pack_f16([[-4,-4],[-4,-4]]))
    calm=computed.sample('wind10',-31.5,115.5,VALID)
    assert calm['value']['speed_kt']==0
    assert calm['value']['direction_from_deg'] is None
    assert 'calm' in calm['direction_reason']


def test_missing_listed_hour_never_skipped(computed):
    path=computed.root/'products/grids/ecmwf_ifs025/runs/20261008T00Z/mslp/20261008T03Z.f16'
    path.unlink()
    for stamp in (VALID,'2026-10-08T03:00:00Z','2026-10-08T04:30:00Z'):
        got=computed.sample('mslp',-31.5,115.5,stamp)
        assert got['value'] is None
        assert 'missing mslp at listed forecast hour 3' in got['reason']
    assert computed.sample('mslp',-31.5,115.5,'2026-10-08T06:00:00Z')['value']==1030


def test_nonuniform_listed_hours_can_interpolate(tmp_path):
    make_run(tmp_path,hours=(144,150),values={'mslp':{144:[[1000]*2]*2,150:[[1012]*2]*2}})
    got=ArchiveStore(tmp_path).sample('mslp',-31.5,115.5,'2026-10-14T03:00:00Z')
    assert got['value']==1006
    assert got['source_hours']==[144,150]


def test_fill_nan_and_zero_weight_cells(computed):
    path=computed.root/'products/grids/ecmwf_ifs025/runs/20261008T00Z/mslp/20261008T00Z.f16'
    path.write_bytes(pack_f16([[1000,float('nan')],[1008,1012]]))
    assert computed.sample('mslp',-31,115,'2026-10-08T00:00:00Z')['value']==1000
    got=computed.sample('mslp',-31.5,115.5,'2026-10-08T00:00:00Z')
    assert got['value'] is None
    assert 'missing grid cell' in got['reason']
    # Raw NaN as well as the packer's fill sentinel must stay missing.
    path.write_bytes(np.array([[1000,float('nan')],[1008,1012]],dtype='<f2').tobytes())
    assert computed.sample('mslp',-31.5,115.5,VALID)['value'] is None


def test_global_wraparound_at_antimeridian(tmp_path):
    make_run(tmp_path,family=GLOBAL_GRID_FAMILY,wrap=True,hours=(0,),values={'mslp':{0:[[1000,1004,1008,1012],[1008,1012,1016,1020]]}})
    store=ArchiveStore(tmp_path)
    at_seam=store.sample('mslp',-31.5,180,'2026-10-08T00:00:00Z')
    assert at_seam['value']==1004
    assert at_seam['value']==store.sample('mslp',-31.5,-180,'2026-10-08T00:00:00Z')['value']
    # x=3.5: halfway from last column (1016) across seam to first (1004).
    assert store.sample('mslp',-31.5,135,'2026-10-08T00:00:00Z')['value']==1010


def test_horizon_outside_domain_and_missing_variable(computed):
    assert computed.sample('mslp',-10,115,VALID)['value'] is None
    assert 'outside' in computed.sample('mslp',-31,115,'2026-10-09T00:00:00Z')['reason']
    unknown=computed.sample('unavailable',-31,115,VALID)
    assert unknown['value'] is None and unknown['units'] is None
    assert 'not retained' in unknown['reason']
    assert computed.sample('mslp',-31,115,VALID,run='20261007T00Z')['run_id'] is None


def test_read_errors_are_null_with_reason(computed):
    base=computed.root/'products/grids/ecmwf_ifs025/runs/20261008T00Z/mslp/20261008T00Z'
    path=base.with_suffix('.f16')
    path.write_bytes(b'bad')
    got=computed.sample('mslp',-31,115,'2026-10-08T00:00:00Z')
    assert got['value'] is None and 'byte count' in got['reason']
    path.write_bytes(pack_f16([[1,2],[3,4]]))
    side=base.with_suffix('.json')
    doc=json.loads(side.read_text()); doc['run']='2026-10-07T12:00:00Z'; side.write_text(json.dumps(doc))
    assert 'does not match' in computed.sample('mslp',-31,115,'2026-10-08T00:00:00Z')['reason']


def test_run_change_and_explicit_family_run(computed):
    older=RUN-timedelta(hours=12)
    make_run(computed.root,run=older,hours=(12,15),values={'mslp':{12:[[990]*2]*2,15:[[996]*2]*2}})
    got=computed.run_change('mslp',-31.5,115.5,VALID)
    assert [row['run_id'] for row in got['runs']]==['20261007T12Z','20261008T00Z']
    assert [row['value'] for row in got['runs']]==[993,1012]
    assert len({row['valid_time_utc'] for row in got['runs']})==1
    assert computed.sample('mslp',-31.5,115.5,VALID,run='ecmwf_ifs025/20261007T12Z')['value']==993


def test_thin_retains_provenance_and_does_not_invent_schedule(tmp_path):
    d=make_run(tmp_path,hours=(0,3),values={'mslp':{0:[[1000]*2]*2,3:[[1012]*2]*2}})
    thin=tmp_path/'products/grids/ecmwf_ifs025/thin/mslp'; thin.mkdir(parents=True)
    for p in (d/'mslp').iterdir():
        shutil.move(str(p),thin/p.name)
    shutil.rmtree(d)
    store=ArchiveStore(tmp_path)
    inventory=store.list_runs()['families'][FAMILY][0]
    assert inventory['locations']==['thin']
    assert inventory['run_id']=='20261008T00Z'
    assert store.sample('mslp',-31,115,'2026-10-08T00:00:00Z')['value']==1000
    assert 'schedule unavailable' in store.sample('mslp',-31,115,VALID)['reason']


def test_profile_returns_only_existing_grid_levels(computed):
    got=computed.profile(-31.5,115.5,VALID)
    assert got['available_pressure_levels_hpa']==[850]
    assert got['levels'][0]['pressure_units']=='hPa'
    assert got['levels'][0]['value']==4
    assert got['levels'][0]['units']=='degC'


def test_section_and_optional_png(computed):
    got=computed.section([[-31,115],[-32,116]],VALID,'mslp',samples=3,png=True)
    assert len(got['samples'])==3
    assert got['samples'][0]['value']==1006
    assert got['samples'][-1]['value']==1018
    assert got['samples'][1]['distance_km']==pytest.approx(got['samples'][-1]['distance_km']/2)
    assert got['png_path'] is None and 'not a project dependency' in got['png_reason']


def test_great_circle_route_crosses_seam_and_handles_duplicate_legs():
    route=route_points([[0,179],[0,-179]],3)
    assert abs(route[1][2])==pytest.approx(180)
    assert route[-1][0]==pytest.approx(222.390160467,rel=1e-8)
    assert route_points([[0,0],[0,0]],3)==[(0,0,0)]*3
    with pytest.raises(ValueError,match='antipodal'):
        route_points([[0,0],[0,180]],3)
    # Great circles bow polewards, rather than linearly interpolating latitude.
    assert route_points([[60,-45],[60,45]],3)[1][1]>60


@pytest.mark.parametrize('arguments',[{'lat':91},{'lon':181},{'lat':True},{'lat':float('nan')},
    {'run':'../../secret'},{'valid_time_utc':'2026-10-08T00:00:00'}, {'family':'other'},{'var':'../secret'}])
def test_invalid_queries_rejected(computed,arguments):
    params=dict(var='mslp',lat=-31,lon=115,valid_time_utc=VALID); params.update(arguments)
    with pytest.raises(ValueError):
        computed.sample(**params)


def test_store_queries_do_not_modify_files(computed):
    def snapshot():
        return {p.relative_to(computed.root):(p.read_bytes(),p.stat().st_mtime_ns) for p in computed.root.rglob('*') if p.is_file()}
    before=snapshot()
    computed.list_runs(); computed.sample('mslp',-31.5,115.5,VALID)
    computed.section([[-31,115],[-32,116]],VALID,'mslp',samples=10)
    computed.run_change('mslp',-31.5,115.5,VALID)
    assert snapshot()==before


def test_wind_missing_components_keep_both_reasons_and_provenance(tmp_path):
    make_run(tmp_path,values={'u10':{0:[[3]*2]*2},'v10':{3:[[4]*2]*2}})
    got=ArchiveStore(tmp_path).sample('wind10',-31.5,115.5,VALID)
    assert got['value'] is None
    assert 'missing u10 at listed forecast hour 3' in got['reason']
    assert 'missing v10 at listed forecast hour 0' in got['reason']
    assert got['component_sources']['u10']['reason'] != got['component_sources']['v10']['reason']
    assert got['source_hours']==[0,3]


def test_profile_reports_levels_retained_only_in_other_runs(computed):
    make_run(computed.root,run=RUN+timedelta(hours=12),values={'mslp':{0:[[1000]*2]*2}})
    got=computed.profile(-31.5,115.5,'2026-10-08T12:00:00Z')
    assert got['run_id']=='20261008T12Z'
    assert got['levels'] is None
    assert got['available_pressure_levels_hpa']==[850]
    assert got['sampled_pressure_levels_hpa']==[]
    assert got['retained_pressure_variables'][0]['run_id']=='20261008T00Z'


def test_section_family_uses_requested_time_run_bounds(computed):
    later=make_run(computed.root,run=RUN+timedelta(hours=12),values={'mslp':{0:[[999]*2]*2}})
    path=later/'manifest.json'; doc=json.loads(path.read_text())
    doc['grid'].update(west=140,east=141); path.write_text(json.dumps(doc))
    got=computed.section([[-31,115],[-32,116]],VALID,'mslp',samples=2)
    assert got['run_id']=='20261008T00Z'
    assert got['family']=='ecmwf_ifs025'
    assert [s['value'] for s in got['samples']]==[1006,1018]
