"""A run is published only when every required artifact is in place.

Readers follow current.json. An incomplete or crashed run stays out of the
manifest, and the scheduler reports that source failed.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest

from isobar_data.grid import REQUIRED, archive_run, grid_shape
from isobar_data.http import Http, Stats
from isobar_data.ledger import build_manifest
from isobar_data.openmeteo import fetch_job
from isobar_data.scheduler import Source, run_sources
from isobar_data.tokens import standard_buckets

UTC = timezone.utc
RUN = datetime(2026, 9, 26, 0, tzinfo=UTC)
OLD = "20260925T12Z"
NEW = "20260926T00Z"


def _index():
    rows = []
    for param, levtype, levelist, _variable, _units in REQUIRED:
        row = {"param": param, "levtype": levtype, "_offset": 0, "_length": 4}
        if levelist is not None:
            row["levelist"] = levelist
        rows.append(row)
    return rows


def _install_grid_fakes(monkeypatch, *, fail_param=None):
    nx, ny = grid_shape()
    units = {
        "msl": "Pa",
        "t": "K",
        "2t": "K",
        "10u": "m s**-1",
        "10v": "m s**-1",
        "tp": "m",
    }

    def load_index(*_args, **_kwargs):
        return _index()

    def load_blob(*args, **kwargs):
        param = args[3]
        if param == fail_param:
            raise RuntimeError(f"crash writing {param}")
        return param.encode()

    def decode(blob, **_kwargs):
        param = blob.decode()
        grid = np.zeros((ny, nx), dtype=np.float64)
        return grid, 0.0, 95.0, -50.0, 170.0, -0.25, 0.25, param, units[param]

    def crop(values, *_args, **_kwargs):
        return np.asarray(values)

    monkeypatch.setattr("isobar_data.grid.LEAD_HOURS", (0,))
    monkeypatch.setattr("isobar_data.grid._load_index", load_index)
    monkeypatch.setattr("isobar_data.grid._load_blob", load_blob)
    monkeypatch.setattr("isobar_data.grid.decode_north_up", decode)
    monkeypatch.setattr("isobar_data.grid.crop_north_up", crop)


def _seed_previous(root):
    directory = root / "products" / "grids" / "ecmwf_ifs025" / "runs" / OLD / "mslp"
    directory.mkdir(parents=True)
    (directory / "20260925T12Z.f16").write_bytes(b"\x00\x00")
    (directory / "20260925T12Z.json").write_text("{}\n")
    pointer = root / "products" / "grids" / "ecmwf_ifs025" / "current.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"latest": OLD, "runs": [OLD]}) + "\n")


def _manifest_paths(root):
    manifest = build_manifest(root, RUN)
    return [item["path"] for item in manifest["products"]]


def test_crash_mid_field_does_not_publish_a_partial_ecmwf_run(tmp_path, monkeypatch):
    _install_grid_fakes(monkeypatch, fail_param="tp")
    _seed_previous(tmp_path)
    summary = archive_run(tmp_path, RUN, consume=lambda: True)
    assert summary["complete"] is False

    paths = _manifest_paths(tmp_path)
    assert not any(NEW in path for path in paths)
    assert any(OLD in path for path in paths)
    pointer = json.loads((tmp_path / "products" / "grids" / "ecmwf_ifs025" / "current.json").read_text())
    assert pointer["runs"] == [OLD]

    def run(ctx):
        return archive_run(ctx["root"], RUN, consume=lambda: True)

    now = RUN + timedelta(hours=8)
    source = Source("ecmwf-open-data", "ECMWF", timedelta(0), 0, None, True, False, run)
    ctx = {"now": now, "state": {}, "buckets": standard_buckets(), "root": tmp_path}
    run_sources([source], ctx)
    assert ctx["state"]["sources"]["ecmwf-open-data"]["status"]["ok"] is False


def test_complete_ecmwf_run_swaps_the_pointer_and_hides_stray_files(tmp_path, monkeypatch):
    _install_grid_fakes(monkeypatch)
    _seed_previous(tmp_path)
    stray = tmp_path / "products" / "grids" / "ecmwf_ifs025" / "runs" / "not-published" / "mslp" / "x.f16"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"\x00\x00")
    summary = archive_run(tmp_path, RUN, consume=lambda: True)
    assert summary["complete"] is True
    pointer = json.loads((tmp_path / "products" / "grids" / "ecmwf_ifs025" / "current.json").read_text())
    assert pointer["latest"] == NEW
    assert OLD in pointer["runs"]
    assert NEW in pointer["runs"]
    paths = _manifest_paths(tmp_path)
    assert any(NEW in path for path in paths)
    assert not any("not-published" in path for path in paths)
    assert build_manifest(tmp_path, RUN)["schema_version"] == 1
    assert build_manifest(tmp_path, RUN)["contract"] == "isobar-data"


def test_older_cycle_published_second_does_not_move_latest_backwards(tmp_path):
    from isobar_data.publish import publish_run

    family = "grids/ecmwf_ifs025"
    for run_id in (NEW, OLD):
        directory = tmp_path / "products" / family / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "marker").write_text("ok\n")
        publish_run(tmp_path, family, run_id, ["marker"], keep="all")
    pointer = json.loads((tmp_path / "products" / family / "current.json").read_text())
    assert pointer["latest"] == NEW
    assert pointer["runs"] == [OLD, NEW]


def test_new_pointers_identify_the_shared_contract_but_legacy_pointers_still_read(tmp_path):
    from isobar_data.publish import publish_run, read_pointer

    family = "kite"
    directory = tmp_path / "products" / family / "runs" / NEW
    directory.mkdir(parents=True)
    (directory / "cottesloe.json").write_text("{}\n")
    publish_run(tmp_path, family, NEW, ["cottesloe.json"])

    pointer_path = tmp_path / "products" / family / "current.json"
    pointer = json.loads(pointer_path.read_text())
    assert pointer["schema_version"] == 1
    assert pointer["contract"] == "isobar-data"
    assert pointer["family"] == family
    assert read_pointer(tmp_path, family)["latest"] == NEW

    pointer_path.write_text(json.dumps({"latest": OLD, "runs": [OLD]}) + "\n")
    assert read_pointer(tmp_path, family) == {"latest": OLD, "runs": [OLD]}

    pointer_path.write_text(json.dumps({"latest": NEW, "runs": [NEW], "schema_version": 2,
                                        "contract": "isobar-data", "family": family}) + "\n")
    from isobar_data.publish import UnsupportedContract
    with pytest.raises(UnsupportedContract):
        publish_run(tmp_path, family, NEW, ["cottesloe.json"])
    assert json.loads(pointer_path.read_text())["schema_version"] == 2


def test_crash_mid_open_meteo_job_does_not_publish_a_mixed_generation(tmp_path, monkeypatch):
    product_dir = tmp_path / "products" / "points" / "ecmwf_ifs"
    product_dir.mkdir(parents=True)
    old = {
        "id": "cottesloe",
        "run": "2026-09-25T00:00:00Z",
        "time": ["2026-09-25T00:00"],
        "hourly": {"wind_speed_10m": [1]},
    }
    for ident in ("cottesloe", "safety-bay"):
        body = dict(old, id=ident)
        (product_dir / f"{ident}.json").write_text(json.dumps(body) + "\n")
    original = (product_dir / "cottesloe.json").read_bytes()

    init = datetime(2026, 9, 26, 0, tzinfo=UTC)
    available = datetime(2026, 9, 26, 7, 22, tzinfo=UTC)
    meta = {
        "last_run_initialisation_time": init.timestamp(),
        "last_run_availability_time": available.timestamp(),
    }
    locations = []
    for latitude in (-32.0, -32.1):
        locations.append({
            "latitude": latitude,
            "longitude": 115.7,
            "hourly": {"time": ["2026-09-26T00:00"], "wind_speed_10m": [9]},
            "hourly_units": {"wind_speed_10m": "kn"},
        })

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("meta.json"):
            return httpx.Response(200, json=meta)
        return httpx.Response(200, json=locations)

    from isobar_data import openmeteo

    real_dump = openmeteo.dump_json
    calls = {"n": 0}

    def dump(path, obj):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("crash before the second point")
        real_dump(path, obj)

    monkeypatch.setattr(openmeteo, "dump_json", dump)
    http = Http(
        standard_buckets(),
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: available + timedelta(minutes=11),
    )
    state = {}
    try:
        fetch_job(
            http,
            tmp_path,
            state,
            available + timedelta(minutes=11),
            job_id="open-meteo-ifs",
            endpoint="https://api.open-meteo.com/v1/forecast",
            meta_urls=("https://api.open-meteo.com/data/ecmwf_ifs/static/meta.json",),
            points=[("cottesloe", -32.0, 115.7), ("safety-bay", -32.1, 115.7)],
            hourly=("wind_speed_10m",),
            model="ecmwf_ifs",
            hours=24,
            days=None,
            cell="nearest",
            wind_kn=True,
            elevation_nan=True,
            native_step=1,
            kind="surface",
            product_dir=product_dir,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("the job should crash before publication")
    http.close()

    assert (product_dir / "cottesloe.json").read_bytes() == original
    assert state.get("sources", {}).get("open-meteo-ifs", {}).get("watermark") is None
    runs = []
    for item in build_manifest(tmp_path, available).get("products") or []:
        if item["id"].startswith("points-ecmwf_ifs-"):
            runs.append(item.get("run") or "")
    assert "2026-09-26T00:00:00Z" not in "".join(runs)
    assert len(set(runs)) <= 1


def _manifest_file(root, manifest, product_id):
    entry = next(item for item in manifest["products"] if item["id"] == product_id)
    return entry, root / entry["path"]


def test_kite_obs_change_does_not_rewrite_the_published_inland_run(tmp_path):
    """A later observation must not edit files current.json already published."""
    from isobar_data.config import load_config
    from isobar_data.kite_job import write_kite_files
    from isobar_data.ledger import build_manifest, write_manifest
    from isobar_data.normalise import connect_obs, insert_obs
    from isobar_data.publish import read_pointer

    inland = tmp_path / "products" / "points" / "ecmwf_ifs" / "perth-airport.json"
    inland.parent.mkdir(parents=True)
    inland.write_text(json.dumps({
        "id": "perth-airport",
        "run": "2026-09-26T00:00:00Z",
        "time": ["2026-09-26T00:00"],
        "hourly": {"wind_speed_10m": [8], "wind_direction_10m": [90], "wind_gusts_10m": [10]},
    }) + "\n")
    config = load_config()
    write_kite_files(tmp_path, config)
    first = read_pointer(tmp_path, "kite")["latest"]
    assert first
    published = tmp_path / "products" / "kite" / "runs" / first
    before = {path: path.read_bytes() for path in published.rglob("*.json")}
    write_manifest(tmp_path, build_manifest(tmp_path, RUN))
    saved = (tmp_path / "manifest.json").read_bytes()

    connection = connect_obs(tmp_path / "products" / "obs" / "obs.sqlite")
    insert_obs(connection, [{
        "wmo": 94614,
        "aifstime_utc": "20260926050000",
        "product_id": "IDW60910",
        "name": "Swanbourne",
        "wind_spd_kt": 12,
    }])
    connection.close()
    write_kite_files(tmp_path, config)

    assert (tmp_path / "manifest.json").read_bytes() == saved
    for path, body in before.items():
        assert path.read_bytes() == body
    assert read_pointer(tmp_path, "kite")["latest"] != first
    fresh = build_manifest(tmp_path, RUN)
    entry, path = _manifest_file(tmp_path, fresh, "kite-cottesloe")
    assert first not in entry["path"]
    assert b"Swanbourne" in path.read_bytes()


def test_chart_and_warning_replacements_leave_manifested_bytes_untouched(tmp_path):
    from isobar_data.bom import store_chart, store_warning
    from isobar_data.ledger import build_manifest, write_manifest

    store_chart(tmp_path, "IDG00073.pdf", b"%PDF-old", "20260926000000")
    store_chart(tmp_path, "IDG00074.gif", b"GIF89a-old", "20260926000000")
    store_warning(tmp_path, "IDW20100.xml", b"<warning>old</warning>", "20260926000000")
    write_manifest(tmp_path, build_manifest(tmp_path, RUN))
    saved = json.loads((tmp_path / "manifest.json").read_text())
    chart, chart_path = _manifest_file(tmp_path, saved, "chart-IDG00073.pdf")
    warning, warning_path = _manifest_file(tmp_path, saved, "warning-IDW20100.xml")
    chart_body = chart_path.read_bytes()
    warning_body = warning_path.read_bytes()

    store_chart(tmp_path, "IDG00073.pdf", b"%PDF-new", "20260926060000")
    store_warning(tmp_path, "IDW20100.xml", b"<warning>new</warning>", "20260926060000")

    assert json.loads((tmp_path / "manifest.json").read_text()) == saved
    assert chart_path.read_bytes() == chart_body
    assert warning_path.read_bytes() == warning_body
    fresh = build_manifest(tmp_path, RUN)
    new_chart, new_chart_path = _manifest_file(tmp_path, fresh, "chart-IDG00073.pdf")
    previous, previous_path = _manifest_file(tmp_path, fresh, "chart-previous-IDG00073.pdf")
    gif, gif_path = _manifest_file(tmp_path, fresh, "chart-IDG00074.gif")
    new_warning, new_warning_path = _manifest_file(tmp_path, fresh, "warning-IDW20100.xml")
    assert new_chart["path"] != chart["path"]
    assert new_chart_path.read_bytes() == b"%PDF-new"
    assert previous_path.read_bytes() == chart_body
    assert gif_path.read_bytes() == b"GIF89a-old"
    assert new_warning["path"] != warning["path"]
    assert new_warning_path.read_bytes() == b"<warning>new</warning>"


def test_aviation_crash_mid_rewrite_leaves_the_published_files(tmp_path, monkeypatch):
    from isobar_data.aviation_feed import write_aerodrome_files
    from isobar_data.ledger import build_manifest

    write_aerodrome_files(tmp_path, ["YPPH", "YSSY"], {})
    first = build_manifest(tmp_path, RUN)
    entry, path = _manifest_file(tmp_path, first, "aviation-YPPH")
    body = path.read_bytes()
    other, other_path = _manifest_file(tmp_path, first, "aviation-YSSY")
    other_body = other_path.read_bytes()
    raw = tmp_path / "raw" / "aviation" / "metar" / "metar.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps([{
        "icaoId": "YPPH",
        "reportTime": "2026-09-26T05:00:00Z",
        "rawOb": "METAR YPPH 260500Z 14013KT 9999",
        "wdir": 140,
        "wspd": 13,
    }]))
    from isobar_data import aviation_feed

    real_dump = aviation_feed.dump_json
    calls = {"n": 0}

    def dump(dest, obj):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("crash before the second aerodrome")
        real_dump(dest, obj)

    monkeypatch.setattr(aviation_feed, "dump_json", dump)
    try:
        write_aerodrome_files(tmp_path, ["YPPH", "YSSY"], {})
    except RuntimeError:
        pass
    else:
        raise AssertionError("the rewrite should crash before publication")
    assert path.read_bytes() == body
    assert other_path.read_bytes() == other_body
    again, again_path = _manifest_file(tmp_path, build_manifest(tmp_path, RUN), "aviation-YPPH")
    assert again["path"] == entry["path"]
    assert again_path.read_bytes() == body
    assert again["sha256"] == entry["sha256"]
