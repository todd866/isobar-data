"""Every request that left the process counts, and a storm stops."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from isobar_data.http import Http, Later, Stats
from isobar_data.ledger import parse_iso
from isobar_data.scheduler import Source, run_sources
from isobar_data.tokens import Bucket, Window, load_buckets, standard_buckets

UTC = timezone.utc
URL = "https://api.open-meteo.com/v1/forecast"


def _client(handler, buckets, now):
    return Http(
        buckets,
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: now,
    )


def test_retry_storm_counts_every_sent_attempt_until_the_bucket_is_empty():
    sent = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        sent["n"] += 1
        return httpx.Response(429, headers={"retry-after": "0"})

    now = datetime(2026, 9, 26, 8, tzinfo=UTC)
    buckets = {"open-meteo": Bucket("open-meteo", (Window("minute", 3, 60),))}
    http = _client(handler, buckets, now)
    try:
        http.get(URL, bucket="open-meteo", weight=1)
    except (Later, RuntimeError):
        pass
    else:
        raise AssertionError("a 429 storm should stop")
    http.close()
    assert sent["n"] == 3
    assert buckets["open-meteo"].used(now.timestamp(), buckets["open-meteo"].windows[0]) == 3
    assert http.stats.open_meteo_calls == 3


def test_retry_after_is_honoured_and_not_followed_by_another_attempt():
    sent = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        sent["n"] += 1
        return httpx.Response(429, headers={"retry-after": "120"})

    now = datetime(2026, 9, 26, 8, tzinfo=UTC)
    buckets = standard_buckets()
    http = _client(handler, buckets, now)
    try:
        http.get(URL, bucket="open-meteo", weight=1)
    except Later as later:
        assert (later.when - now).total_seconds() == 120
        assert later.failure is True
    else:
        raise AssertionError("Retry-After should defer the source")
    http.close()
    assert sent["n"] == 1
    day = next(window for window in buckets["open-meteo"].windows if window.name == "day")
    assert buckets["open-meteo"].used(now.timestamp(), day) == 1


def test_retry_storm_persists_backoff_and_opens_a_circuit(tmp_path):
    sent = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        sent["n"] += 1
        return httpx.Response(429, headers={"retry-after": "600"})

    def run(ctx):
        ctx["http"].get(URL, bucket="open-meteo", weight=1)
        return {"ok": True, "complete": True}

    source = Source("open-meteo-ifs", "IFS 9 km", timedelta(0), 0, None, False, False, run)
    moment = datetime(2026, 9, 26, 8, tzinfo=UTC)
    state = {}
    buckets = standard_buckets()
    for attempt in range(5):
        http = _client(handler, buckets, moment)
        ctx = {"now": moment, "state": state, "buckets": buckets, "root": tmp_path, "http": http}
        run_sources([source], ctx)
        http.close()
        assert sent["n"] == attempt + 1
        if attempt < 4:
            moment = parse_iso(state["sources"]["open-meteo-ifs"]["not_before"])

    saved = json.loads((tmp_path / "state.json").read_text())
    fresh = load_buckets(saved["buckets"])
    day = next(window for window in fresh["open-meteo"].windows if window.name == "day")
    assert fresh["open-meteo"].used(moment.timestamp(), day) == 5
    http = _client(handler, fresh, moment)
    ctx = {
        "now": moment,
        "state": saved,
        "buckets": fresh,
        "root": tmp_path,
        "http": http,
    }
    reports = run_sources([source], ctx)
    http.close()
    assert sent["n"] == 5
    assert reports[0]["skipped"] == "circuit"
