"""Conditional GET, jitter, and the Bureau HTTP ban."""

from datetime import datetime, timezone

import httpx

from isobar_data.aviation_feed import _conditional, commit_validator
from isobar_data.http import BACKOFF_CAP, Http, Later, Stats
from isobar_data.identity import USER_AGENT
from isobar_data.policy import PolicyError
from isobar_data.tokens import standard_buckets


UTC = timezone.utc


def _http(handler, uniform=None):
    buckets = standard_buckets()
    client = Http(
        buckets,
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=uniform or (lambda _a, _b: 0),
        now=lambda: datetime(2026, 9, 26, 8, tzinfo=UTC),
    )
    return client, buckets


def test_not_modified_sends_the_validator_and_writes_nothing(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["agent"] = request.headers["user-agent"]
        seen["match"] = request.headers.get("if-none-match")
        if seen["match"] == '"abc"':
            return httpx.Response(304)
        return httpx.Response(200, content=b'{"ok":true}', headers={"etag": '"abc"'})

    http, _buckets = _http(handler)
    state = {}
    url = "https://aviationweather.gov/api/data/metar?ids=YPPH"
    status, body, etag, modified = _conditional(http, url, state, "metar")
    destination = tmp_path / "metar.json"
    if body is not None:
        destination.write_bytes(body)
        commit_validator(state, "metar", etag, modified)
    assert status == 200
    assert destination.is_file()
    status, body, _etag, _modified = _conditional(http, url, state, "metar")
    if body is not None:
        destination.write_bytes(body + b"-rewritten")
    assert status == 304
    assert body is None
    assert destination.read_bytes() == b'{"ok":true}'
    assert seen["match"] == '"abc"'
    assert seen["agent"] == USER_AGENT
    assert "Mozilla" not in seen["agent"]
    http.close()


def test_a_failed_attempt_counts_and_is_not_refunded():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, content=b"busy")
        return httpx.Response(200, content=b"ok")

    http, buckets = _http(handler)
    response = http.get("https://api.open-meteo.com/v1/forecast", bucket="open-meteo", weight=1)
    assert response.status_code == 200
    assert calls["n"] == 2
    window = buckets["open-meteo"].windows[0]
    assert buckets["open-meteo"].used(http.now().timestamp(), window) == 2
    assert http.stats.open_meteo_calls == 2
    http.close()


def test_backoff_is_full_jitter_capped_at_thirty_minutes():
    slept = []
    http, _buckets = _http(lambda _request: httpx.Response(200), uniform=lambda _a, b: b)
    http.sleep = slept.append
    try:
        http._wait_or_defer(0, BACKOFF_CAP * 4, "open-meteo", 1)
    except Later as later:
        assert (later.when - http.now()).total_seconds() == BACKOFF_CAP
    else:
        raise AssertionError("a long jitter should be deferred")
    assert slept == []
    assert BACKOFF_CAP == 1800
    http.close()


def test_each_redirect_hop_is_charged_before_the_request():
    from isobar_data.tokens import Bucket, Window

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "https://aviationweather.gov/api/data/metar"})
        return httpx.Response(200, content=b"ok")

    buckets = {"aviationweather": Bucket("aviationweather", (Window("minute", 2, 60),))}
    http = Http(
        buckets,
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: datetime(2026, 9, 26, 8, tzinfo=UTC),
    )
    response = http.get("https://aviationweather.gov/start", bucket="aviationweather", weight=1)
    assert response.status_code == 200
    assert len(seen) == 2
    window = buckets["aviationweather"].windows[0]
    assert buckets["aviationweather"].used(http.now().timestamp(), window) == 2
    http.close()


def test_a_redirect_hop_is_not_sent_when_the_bucket_cannot_pay():
    from isobar_data.tokens import Bucket, Window

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://aviationweather.gov/hop"})

    buckets = {"aviationweather": Bucket("aviationweather", (Window("minute", 1, 60),))}
    http = Http(
        buckets,
        Stats(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        uniform=lambda _a, _b: 0,
        now=lambda: datetime(2026, 9, 26, 8, tzinfo=UTC),
    )
    try:
        http.get("https://aviationweather.gov/start", bucket="aviationweather", weight=1)
    except (Later, RuntimeError):
        pass
    else:
        raise AssertionError("the unpaid redirect hop should stop the exchange")
    http.close()
    assert seen == ["https://aviationweather.gov/start"]
    window = buckets["aviationweather"].windows[0]
    assert buckets["aviationweather"].used(http.now().timestamp(), window) == 1


def test_bureau_http_hosts_are_refused():
    http, _buckets = _http(lambda _request: httpx.Response(200))
    for url in (
        "https://www.bom.gov.au/fwo/IDW60910.json",
        "https://api.weather.bom.gov.au/v1/locations",
    ):
        try:
            http.get(url, bucket="open-meteo", weight=1)
        except PolicyError:
            pass
        else:
            raise AssertionError(url)
    http.close()
