"""Week-long ECMWF grid: explicit hours, fail closed, certifi TLS.

Expectations are hand-listed. A missing listed hour must not publish a shorter
week, and hours that ECMWF does not publish must not appear as files.
"""

import json
import ssl
from datetime import datetime, timedelta, timezone
from urllib import request as urllib_request

import numpy as np
import pytest

from isobar_data.fetch_ecmwf import fetch_bytes, lead_hours
from isobar_data.grid import GLOBAL_GRID_FAMILY, GRID_FAMILY, GridSpec, LEAD_HOURS, REQUIRED, archive_run, cycle_pair, grid_spec, sidecar
from isobar_data.fetch_ecmwf import crop_north_up

UTC = timezone.utc
RUN = datetime(2026, 10, 6, 12, tzinfo=UTC)

# 0–144 h every 3 h, then 150, 156, 162, 168. 49 + 4 = 53.
WEEK_HOURS = (
    0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57,
    60, 63, 66, 69, 72, 75, 78, 81, 84, 87, 90, 93, 96, 99, 102, 105, 108, 111,
    114, 117, 120, 123, 126, 129, 132, 135, 138, 141, 144, 150, 156, 162, 168,
)


def _cn(context: ssl.SSLContext) -> set[str]:
    names = set()
    for cert in context.get_ca_certs():
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    names.add(value)
    return names


def _install_fakes(monkeypatch, *, missing_steps=()):
    """Decode without GRIB. ``missing_steps`` are portal 404s."""
    import urllib.error

    nx, ny = 2, 2
    units = {
        "msl": "Pa",
        "t": "K",
        "2t": "K",
        "10u": "m s**-1",
        "10v": "m s**-1",
        "tp": "m",
    }
    monkeypatch.setattr("isobar_data.grid.grid_shape", lambda: (nx, ny))

    def load_index(_raw, _run, step, consume=None):
        if step in missing_steps:
            raise urllib.error.HTTPError("https://data.ecmwf.int/missing", 404, "missing", hdrs=None, fp=None)
        rows = []
        for param, levtype, levelist, _variable, _units in REQUIRED:
            row = {"param": param, "levtype": levtype, "_offset": 0, "_length": 4}
            if levelist is not None:
                row["levelist"] = levelist
            rows.append(row)
        return rows

    def load_blob(*args, **kwargs):
        return args[3].encode()

    def decode(blob, **_kwargs):
        param = blob.decode()
        grid = np.zeros((ny, nx), dtype=np.float64)
        return grid, 0.0, 95.0, -50.0, 170.0, -0.25, 0.25, param, units[param]

    def crop(values, *_args, **_kwargs):
        return np.zeros((ny, nx), dtype=np.float32)

    monkeypatch.setattr("isobar_data.grid._load_index", load_index)
    monkeypatch.setattr("isobar_data.grid._load_blob", load_blob)
    monkeypatch.setattr("isobar_data.grid.decode_north_up", decode)
    monkeypatch.setattr("isobar_data.grid.crop_north_up", crop)


def _pointer(root):
    path = root / "products" / "grids" / "ecmwf_ifs025" / "current.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _manifest(root, run):
    stamp = run.strftime("%Y%m%dT%HZ")
    path = root / "products" / "grids" / "ecmwf_ifs025" / "runs" / stamp / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def test_published_ladder_is_three_hourly_through_144_then_six_hourly_to_168():
    assert LEAD_HOURS == WEEK_HOURS
    assert 147 not in LEAD_HOURS
    assert 153 not in LEAD_HOURS
    assert LEAD_HOURS[LEAD_HOURS.index(144) + 1] == 150


def test_complete_run_publishes_schema_2_with_the_explicit_hour_list(tmp_path, monkeypatch):
    _install_fakes(monkeypatch)
    summary = archive_run(tmp_path, RUN, consume=lambda: True)
    assert summary["complete"] is True
    again = archive_run(tmp_path, RUN, consume=lambda: True)
    assert again["complete"] is True
    pointer = _pointer(tmp_path)
    manifest = _manifest(tmp_path, RUN)
    assert pointer["schema_version"] == 2
    assert pointer["contract"] == "isobar-data"
    assert pointer["family"] == "grids/ecmwf_ifs025"
    assert pointer["forecast_hours"] == list(WEEK_HOURS)
    assert manifest["schema_version"] == 2
    assert manifest["forecast_hours"] == list(WEEK_HOURS)
    assert manifest["horizon_hours"] == 168
    assert "uniform_step_hours" not in manifest or manifest["uniform_step_hours"] is None
    stamp = "20261006T12Z"
    mslp = tmp_path / "products" / "grids" / "ecmwf_ifs025" / "runs" / stamp / "mslp"
    names = sorted(path.name for path in mslp.glob("*.f16"))
    want = []
    for hour in WEEK_HOURS:
        valid = RUN + timedelta(hours=hour)
        want.append(valid.strftime("%Y%m%dT%HZ") + ".f16")
    assert names == want
    assert not (mslp / "20261012T15Z.f16").exists()  # run+147 h, not a published step
    side = json.loads((mslp / "20261012T18Z.json").read_text())  # run+150 h
    assert side["lead_hours"] == 150
    assert side.get("native_step_hours") != 3


def test_an_hour_between_six_hour_steps_is_not_a_lead():
    with pytest.raises(ValueError):
        lead_hours(RUN, RUN + timedelta(hours=147))
    assert lead_hours(RUN, RUN + timedelta(hours=144)) == 144
    assert lead_hours(RUN, RUN + timedelta(hours=150)) == 150
    assert lead_hours(RUN, RUN + timedelta(hours=168)) == 168


def test_sidecar_lead_for_a_six_hour_step_is_that_hour():
    side = sidecar(RUN, RUN + timedelta(hours=168), "hPa", "msl")
    assert side["lead_hours"] == 168
    assert side.get("native_step_hours") != 3


def test_global_grid_contract_covers_both_poles_without_duplicate_seam():
    spec = grid_spec(GLOBAL_GRID_FAMILY)
    assert (spec.west, spec.east, spec.north, spec.south, spec.step) == (-180, 179.5, 90, -90, 0.5)
    assert (spec.nx, spec.ny, spec.wraps_longitude) == (720, 361, True)
    side = sidecar(RUN, RUN, "hPa", "msl", GLOBAL_GRID_FAMILY)
    assert side["nx"] == 720 and side["ny"] == 361
    assert side["wraps_longitude"] is True
    assert side["source_grid_step"] == 0.25 and side["target_grid_step"] == 0.5


def test_global_downsample_crosses_dateline_and_keeps_poles():
    source = np.arange(721 * 1440, dtype=np.float32).reshape(721, 1440)
    output = crop_north_up(source, 90, 0, -90, 359.75,
        west=-180, east=179.5, north=90, south=-90, step=0.5,
        dlat=-0.25, dlon=0.25)
    assert output.shape == (361, 720)
    assert output[0, 0] == source[0, 720]
    assert output[-1, -1] == source[-1, 718]
    assert output[0, 1] == source[0, 722]


def test_global_archive_resume_reuses_cached_fields(tmp_path, monkeypatch):
    spec = GridSpec(GLOBAL_GRID_FAMILY, -180, 179.5, 90, -90, 0.5, 2, 2, True)
    monkeypatch.setattr("isobar_data.grid.grid_spec", lambda family=GRID_FAMILY: spec if family == GLOBAL_GRID_FAMILY else grid_spec(family))
    rows = [{"param": param, "levtype": levtype, "_offset": 0, "_length": 4, **({"levelist": levelist} if levelist else {})} for param, levtype, levelist, _name, _unit in REQUIRED]
    calls = {"blob": 0}
    monkeypatch.setattr("isobar_data.grid._load_index", lambda *_args, **_kwargs: rows)
    def blob(*args, **kwargs):
        calls["blob"] += 1
        return args[3].encode()
    monkeypatch.setattr("isobar_data.grid._load_blob", blob)
    units = {"msl": "Pa", "t": "K", "2t": "K", "10u": "m s**-1", "10v": "m s**-1", "tp": "m"}
    monkeypatch.setattr("isobar_data.grid.decode_north_up", lambda blob, **_kwargs: (np.zeros((2, 2)), 90, 0, -90, 359.75, -0.25, 0.25, blob.decode(), units[blob.decode()]))
    monkeypatch.setattr("isobar_data.grid.crop_north_up", lambda values, *args, **kwargs: np.zeros((2, 2), dtype=np.float32))
    first = archive_run(tmp_path, RUN, consume=lambda: True, family=GLOBAL_GRID_FAMILY)
    before = calls["blob"]
    second = archive_run(tmp_path, RUN, consume=lambda: True, family=GLOBAL_GRID_FAMILY)
    assert first["complete"] and second["complete"]
    assert calls["blob"] == before
    pointer = json.loads((tmp_path / "products" / GLOBAL_GRID_FAMILY / "current.json").read_text())
    assert pointer["family"] == GLOBAL_GRID_FAMILY


def test_global_invalid_short_cycle_does_not_publish(tmp_path, monkeypatch):
    _install_fakes(monkeypatch)
    run = datetime(2026, 10, 6, 18, tzinfo=UTC)
    summary = archive_run(tmp_path, run, consume=lambda: True, family=GLOBAL_GRID_FAMILY)
    assert summary["complete"] is False
    assert not (tmp_path / "products" / GLOBAL_GRID_FAMILY / "current.json").exists()


def test_missing_168h_step_does_not_publish_a_shorter_week(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, missing_steps={168})
    summary = archive_run(tmp_path, RUN, consume=lambda: True)
    assert summary["complete"] is False
    assert 168 in summary["missing_steps"]
    pointer = _pointer(tmp_path)
    assert pointer is None or pointer.get("latest") != "20261006T12Z"
    stamp = "20261006T12Z"
    mslp = tmp_path / "products" / "grids" / "ecmwf_ifs025" / "runs" / stamp / "mslp"
    # The absent tail is not invented, and a gap is not stored as zeros.
    assert not (mslp / "20261013T12Z.f16").exists()
    staged = sorted(path.name for path in mslp.glob("*.f16")) if mslp.is_dir() else []
    assert "20261013T12Z.f16" not in staged
    assert all(name in { (RUN + timedelta(hours=hour)).strftime("%Y%m%dT%HZ") + ".f16" for hour in WEEK_HOURS } for name in staged)


def test_18z_is_not_published_even_when_every_step_would_decode(tmp_path, monkeypatch):
    _install_fakes(monkeypatch)
    run = datetime(2026, 10, 6, 18, tzinfo=UTC)
    summary = archive_run(tmp_path, run, consume=lambda: True)
    assert summary["complete"] is False
    assert _pointer(tmp_path) is None
    assert not list((tmp_path / "products").rglob("*.f16"))


def test_a_newer_grid_schema_is_not_republished(tmp_path):
    from isobar_data.publish import UnsupportedContract, publish_run

    family = "grids/ecmwf_ifs025"
    directory = tmp_path / "products" / family / "runs" / "20261006T12Z"
    directory.mkdir(parents=True)
    (directory / "marker").write_text("ok\n")
    pointer = tmp_path / "products" / family / "current.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({
        "latest": "20261006T12Z",
        "runs": ["20261006T12Z"],
        "schema_version": 3,
        "contract": "isobar-data",
        "family": family,
    }) + "\n")
    with pytest.raises(UnsupportedContract):
        publish_run(tmp_path, family, "20261006T12Z", ["marker"], keep="all")
    assert json.loads(pointer.read_text())["schema_version"] == 3


def test_cycle_pair_prefers_00z_and_12z_over_a_newer_18z():
    now = datetime(2026, 10, 6, 20, tzinfo=UTC)
    first, second = cycle_pair(now)
    assert (first, second) == (
        datetime(2026, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 10, 6, 12, tzinfo=UTC),
    )


def test_downloader_trusts_harica_from_certifi_not_ssl_cert_file(tmp_path, monkeypatch):
    decoy = tmp_path / "decoy.pem"
    decoy.write_text("this is not a CA bundle\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(decoy))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    captured = {}

    class _Response:
        status = 200

        def read(self):
            return b"{}\n"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(req, timeout=None, context=None):
        captured["context"] = context
        captured["url"] = req.full_url
        return _Response()

    monkeypatch.setattr(urllib_request, "urlopen", urlopen)
    body = fetch_bytes("https://data.ecmwf.int/forecasts/example.index")
    assert body == b"{}\n"
    context = captured["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert "HARICA TLS RSA Root CA 2021" in _cn(context)
    with pytest.raises(ssl.SSLError):
        ssl.create_default_context(cafile=str(decoy))


def test_a_stored_schema_1_field_is_fetched_again_not_republished(tmp_path, monkeypatch):
    """A 0-96 h run stored before schema 2 must not publish its old sidecars."""
    _install_fakes(monkeypatch)
    assert archive_run(tmp_path, RUN, consume=lambda: True)["complete"] is True
    runs = tmp_path / "products" / "grids" / "ecmwf_ifs025" / "runs" / "20261006T12Z"
    legacy = 0
    for side_path in runs.glob("*/*.json"):
        side = json.loads(side_path.read_text())
        if side.get("lead_hours", 999) <= 96:
            side.pop("lead_hours")
            side["native_step_hours"] = 3
            side_path.write_text(json.dumps(side))
            legacy += 1
    assert legacy
    again = archive_run(tmp_path, RUN, consume=lambda: True)
    assert again["complete"] is True
    assert again["stored"] >= legacy
    for side_path in runs.glob("*/*.json"):
        side = json.loads(side_path.read_text())
        assert "native_step_hours" not in side, side_path
        assert isinstance(side["lead_hours"], int), side_path
