"""Validators and FTP watermarks move only after the bytes are stored."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from isobar_data.aviation_feed import fetch_metar_taf
from isobar_data.bom import sync_charts, sync_obs, sync_warnings
from isobar_data.http import Http, Stats
from isobar_data.scheduler import Source, run_sources
from isobar_data.tokens import standard_buckets

UTC = timezone.utc


class _Ftp:
    def __init__(self, name, body):
        self.name = name
        self.body = body
        self.retr_count = 0

    def size(self, _name):
        return len(self.body)

    def mdtm(self, _name):
        return "20260926050000"

    def retr(self, _name):
        self.retr_count += 1
        return self.body

    def nlst(self):
        return [self.name]

    def close(self):
        return None


def test_obs_crash_before_unpack_does_not_advance_the_checkpoint(tmp_path, monkeypatch):
    from isobar_data import bom

    def boom(*_args, **_kwargs):
        raise RuntimeError("crash before unpack")

    monkeypatch.setattr(bom, "unpack_obs", boom)
    session = _Ftp("IDW60910.tgz", b"not-a-tarball")
    state = {}
    try:
        sync_obs(tmp_path, session, ("IDW60910.tgz",), state)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unpack should have crashed")
    assert "IDW60910.tgz" not in state.get("ftp", {})

    monkeypatch.setattr(bom, "unpack_obs", lambda *_args, **_kwargs: {"inserted": 0, "coords": {}})
    sync_obs(tmp_path, session, ("IDW60910.tgz",), state)
    assert session.retr_count == 2
    assert state["ftp"]["IDW60910.tgz"]["mdtm"] == "20260926050000"


def test_chart_crash_before_publish_does_not_advance_the_checkpoint(tmp_path, monkeypatch):
    from isobar_data import bom

    real_store = bom.store_chart

    def boom(*_args, **_kwargs):
        raise RuntimeError("crash before the chart is stored")

    monkeypatch.setattr(bom, "store_chart", boom)
    session = _Ftp("IDG00073.pdf", b"%PDF-1.4")
    state = {}
    try:
        sync_charts(tmp_path, session, ("IDG00073.pdf",), state)
    except RuntimeError:
        pass
    else:
        raise AssertionError("store_chart should have crashed")
    assert "IDG00073.pdf" not in state.get("ftp", {})
    monkeypatch.setattr(bom, "store_chart", real_store)
    sync_charts(tmp_path, session, ("IDG00073.pdf",), state)
    assert session.retr_count == 2


def test_warning_crash_before_publish_does_not_advance_the_checkpoint(tmp_path, monkeypatch):
    from isobar_data import bom

    def boom(*_args, **_kwargs):
        raise RuntimeError("crash before the warning is stored")

    monkeypatch.setattr(bom, "store_warning", boom)
    session = _Ftp("IDW20100.xml", b"<warning/>")
    state = {}
    try:
        sync_warnings(tmp_path, session, state)
    except RuntimeError:
        pass
    else:
        raise AssertionError("store_warning should have crashed")
    assert "IDW20100.xml" not in state.get("ftp", {})


def test_aviation_crash_before_parse_does_not_commit_the_validator(tmp_path):
    calls = {"n": 0, "match": "unset"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["match"] = request.headers.get("if-none-match")
        if calls["n"] == 1:
            return httpx.Response(200, content=b"not-json", headers={"etag": '"abc"'})
        payload = [{"icaoId": "YPPH", "reportTime": "2026-09-26T05:00:00Z", "rawOb": "METAR YPPH"}]
        return httpx.Response(200, content=json.dumps(payload).encode(), headers={"etag": '"abc"'})

    now = datetime(2026, 9, 26, 8, tzinfo=UTC)
    buckets = standard_buckets()
    state = {}

    def run(ctx):
        return fetch_metar_taf(ctx["http"], ctx["root"], ctx["state"], ["YPPH"], which="metar")

    http = Http(
        buckets,
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: now,
    )
    source = Source("aviation-metar", "METAR", timedelta(0), 0, None, False, False, run)
    ctx = {"now": now, "state": state, "buckets": buckets, "root": tmp_path, "http": http}
    run_sources([source], ctx)
    http.close()
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "metar" not in saved.get("validators", {})
    assert saved["sources"]["aviation-metar"].get("not_before")

    http = Http(
        buckets,
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: now + timedelta(hours=2),
    )
    ctx["http"] = http
    ctx["now"] = now + timedelta(hours=2)
    run_sources([source], ctx)
    http.close()
    assert calls["match"] is None
    assert (tmp_path / "raw" / "aviation" / "metar" / "metar.json").exists() or (
        tmp_path / "raw" / "aviation" / "metar" / "metar.json.zst"
    ).exists()
