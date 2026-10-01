import json
import io
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from isobar_data.aviation_feed import publish_aviation
from isobar_data.ledger import build_manifest, write_attribution
from isobar_data.notac import MAX_BODY_BYTES, NotacError, fetch_notams
from isobar_data.publish import pointer_path, published_path


TOKEN = "lb_" + "a" * 40
NOW = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)


def raw(number: str, location: str = "YPPH", end: str = "2609270600", *, permanent: bool = False, estimated: bool = False, start: str = "2609270100", kind: str = "NOTAMN", replaces: str | None = None) -> str:
    closing = "PERM" if permanent else end + (" EST" if estimated else "")
    predecessor = f" {replaces}" if replaces else ""
    return f"""{number} {kind}{predecessor}
Q) {location}/QMRLC/IV/NBO/A/000/999/3156S11558E005
A) {location}
B) {start}
C) {closing}
E) RWY 03/21 CLSD FOR WORKS.
"""


def row(number: str, location: str = "YPPH", *, with_raw: bool = True, permanent: bool = False, status: str = "active", start: str = "2026-09-27T01:00:00Z") -> dict:
    value = {
        "id": f"00000000-0000-0000-0000-{int(number[1:5]):012d}",
        "number": number,
        "location_code": location,
        "affected_fir": "YMMM",
        "effective_start": start,
        "effective_end": None if permanent else "2026-09-27T06:00:00Z",
        "status": status,
        "text": "RWY 03/21 CLSD FOR WORKS.",
        "category": {"code": "RUNWAY", "label": "Runway"},
        "readings": [{"short": "Provider generated reading that must not replace ICAO text."}],
    }
    if with_raw:
        value["raw"] = raw(number, location, permanent=permanent)
    return value


def response(results: list[dict], *, count: int | None = None, next_url: str | None = None) -> dict:
    return {"count": len(results) if count is None else count, "next": next_url, "previous": None, "results": results}


def request_path(request: httpx.Request) -> str:
    return str(request.url)


def assert_search_request(request: httpx.Request, locations: str = "YPPH,YPJT,YMMM") -> None:
    assert request.method == "GET"
    assert request.url.host == "notac.aero"
    assert request.url.path == "/api/v1/notam/"
    query = parse_qs(request.url.query.decode())
    assert query["location"] == [locations]
    assert query["sort"] == ["oldest"]
    assert query["status"] == ["any"]
    assert query["valid_from"] == ["2026-09-27T00:00:00Z"]
    assert query["valid_to"] == ["2026-09-30T00:00:00Z"]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_fetches_pages_detail_fallback_and_publishes_authoritative_raw(tmp_path):
    next_url = "https://notac.aero/api/v1/notam/?location=YPPH%2CYPJT%2CYMMM&sort=oldest&status=any&valid_from=2026-09-27T00%3A00%3A00Z&valid_to=2026-09-30T00%3A00%3A00Z&page=2"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request_path(request))
        if request.url.path == "/api/v1/notam/":
            assert_search_request(request)
            page = request.url.params.get("page", "1")
            if page == "1":
                return httpx.Response(200, json=response([row("A1234/26")], count=3, next_url=next_url))
            assert page == "2"
            return httpx.Response(200, json=response([row("A1235/26", "YPJT", with_raw=False), row("A1236/26", "YMMM", permanent=True)], count=3))
        if request.url.path == "/api/v1/notam/00000000-0000-0000-0000-000000001235/":
            return httpx.Response(200, json=row("A1235/26", "YPJT", permanent=False))
        raise AssertionError(f"unexpected request: {request.url}")

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))

    assert result["ok"] is True
    assert result["count"] == 3
    assert result["counts_by_location"] == {"YMMM": 1, "YPJT": 1, "YPPH": 1}
    assert result["requests"] == 3
    assert result["credits_used"] == 5
    assert result["published"] is True
    assert result["source"] == "NOTAC · unofficial"
    assert result["coverage"] == {
        "locations": ["YPPH", "YPJT", "YMMM"],
        "valid_from": "2026-09-27T00:00:00Z",
        "valid_to": "2026-09-30T00:00:00Z",
    }
    product = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())
    assert product["source"] == "NOTAC · unofficial"
    assert product["licence_id"] == "notac-terms"
    assert product["attribution"] == ["NOTAC · unofficial"]
    entry = next(item for item in build_manifest(tmp_path, NOW)["products"] if item["id"] == "aviation-notams")
    assert entry["licence_ids"] == ["notac-terms"]
    write_attribution(tmp_path)
    sources = json.loads((tmp_path / "attribution.json").read_text())["sources"]
    credit = next(item for item in sources if item["licence_id"] == entry["licence_id"])
    assert credit["policy_url"] == "https://notac.aero/terms/"
    assert "NOTAC" in credit["attribution"]
    assert credit["redistribute"] is False
    assert [notice["id"] for notice in product["notices"]] == ["A1234/26", "A1235/26", "A1236/26"]
    assert product["notices"][0]["raw"] == raw("A1234/26")
    assert product["notices"][1]["provider_id"] == "00000000-0000-0000-0000-000000001235"
    assert product["notices"][1]["affected_fir"] == "YMMM"
    assert product["notices"][1]["title"] == "Runway 03/21 closed FOR WORKS."
    assert "Provider generated" not in product["notices"][1]["title"]
    assert product["notices"][2]["permanent"] is True
    assert {urlparse(call).path for call in calls} == {"/api/v1/notam/", "/api/v1/notam/00000000-0000-0000-0000-000000001235/"}


def test_empty_complete_snapshot_replaces_stale_notams(tmp_path):
    publish_aviation(tmp_path, {"notams.json": {"schema_version": 1, "source": "old", "notices": [{"id": "OLD"}]}, "weather.json": {"ok": True}})

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([], count=0))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))

    assert result["ok"] is True and result["count"] == 0 and result["published"] is True
    product = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())
    assert product["notices"] == []
    assert json.loads(published_path(tmp_path, "aviation", "weather.json").read_text()) == {
        "ok": True, "schema_version": 1, "contract": "isobar-data", "family": "aviation",
    }


def test_failure_after_first_page_preserves_previous_pointer_and_data(tmp_path):
    publish_aviation(tmp_path, {"notams.json": {"schema_version": 1, "source": "old", "notices": [{"id": "OLD"}]}})
    before_pointer = pointer_path(tmp_path, "aviation").read_bytes()
    before_product = published_path(tmp_path, "aviation", "notams.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/notam/" and request.url.params.get("page", "1") == "1":
            assert_search_request(request)
            return httpx.Response(200, json=response([row("A1234/26")], count=2, next_url="https://notac.aero/api/v1/notam/?location=YPPH%2CYPJT%2CYMMM&sort=oldest&status=any&valid_from=2026-09-27T00%3A00%3A00Z&valid_to=2026-09-30T00%3A00%3A00Z&page=2"))
        return httpx.Response(500, text="upstream unavailable")

    with pytest.raises(NotacError, match="500"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert pointer_path(tmp_path, "aviation").read_bytes() == before_pointer
    assert published_path(tmp_path, "aviation", "notams.json").read_bytes() == before_product


def test_dry_run_does_not_touch_disk(tmp_path):
    publish_aviation(tmp_path, {"notams.json": {"schema_version": 1, "source": "old", "notices": [{"id": "OLD"}]}})
    before = pointer_path(tmp_path, "aviation").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([row("A1234/26")]))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, publish=False, transport=httpx.MockTransport(handler))
    assert result["count"] == 1 and result["published"] is False
    assert pointer_path(tmp_path, "aviation").read_bytes() == before
    assert json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["source"] == "old"


@pytest.mark.parametrize("status", [401, 402, 429])
def test_provider_status_errors_fail_without_retry_or_token_leak(tmp_path, status):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert_search_request(request)
        return httpx.Response(status, text=f"failure {TOKEN}")

    with pytest.raises(NotacError) as error:
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert calls == 1
    assert TOKEN not in str(error.value)


def test_hostile_pagination_url_is_rejected_without_following_or_leaking_token(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert_search_request(request)
        return httpx.Response(200, json=response([row("A1234/26")], count=2, next_url="https://evil.example/notams?page=2"))

    with pytest.raises(NotacError, match="page") as error:
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert calls == 1
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("payload", [
    {"count": 2, "next": None, "previous": None, "results": [row("A1234/26")]},
    {"count": 1, "next": None, "previous": None, "results": [{"number": "not-a-notam"}]},
    {"count": 1, "next": None, "previous": None, "results": [row("A1234/26", with_raw=False)]},
])
def test_malformed_or_incomplete_feed_fails_before_publish(tmp_path, payload):
    publish_aviation(tmp_path, {"notams.json": {"schema_version": 1, "source": "old", "notices": [{"id": "OLD"}]}})
    before = pointer_path(tmp_path, "aviation").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/notam/":
            assert_search_request(request)
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"id": "detail-without-raw"})

    with pytest.raises(NotacError):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert pointer_path(tmp_path, "aviation").read_bytes() == before


def test_request_and_argument_limits_are_enforced(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([row("A1234/26")], count=2, next_url="https://notac.aero/api/v1/notam/?location=YPPH%2CYPJT%2CYMMM&sort=oldest&status=any&valid_from=2026-09-27T00%3A00%3A00Z&valid_to=2026-09-30T00%3A00%3A00Z&page=2"))

    with pytest.raises(NotacError, match="request limit"):
        fetch_notams(tmp_path, TOKEN, now=NOW, max_requests=1, transport=httpx.MockTransport(handler))
    for kwargs in ({"token": "bad"}, {"locations": ()}, {"locations": ("YPPH", "bad")}, {"hours": 0}, {"hours": 721}, {"max_requests": 0}):
        with pytest.raises(ValueError):
            fetch_notams(tmp_path, kwargs.pop("token", TOKEN), now=NOW, transport=httpx.MockTransport(handler), **kwargs)


def test_upcoming_status_and_future_window_are_preserved(tmp_path):
    future = row("A1237/26", status="upcoming", start="2026-09-28T01:00:00Z")
    future["raw"] = raw("A1237/26", start="2609280100", end="2609280600")

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([future]))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    notice = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["notices"][0]
    assert result["count"] == 1
    assert notice["provider_status"] == "upcoming"
    assert notice["effective_from"] == "2026-09-28T01:00:00Z"


def test_detail_identity_number_or_status_change_aborts_snapshot(tmp_path):
    listed = row("A1234/26", with_raw=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/notam/":
            assert_search_request(request)
            return httpx.Response(200, json=response([listed]))
        changed = row("A1235/26", status="upcoming")
        changed["id"] = listed["id"]
        return httpx.Response(200, json=changed)

    with pytest.raises(NotacError, match="changed|different"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))


def test_redirect_is_never_followed(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert_search_request(request)
        return httpx.Response(302, headers={"location": "https://evil.example/notams"})

    with pytest.raises(NotacError, match="302"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert len(calls) == 1


def test_malformed_json_is_rejected_without_publish(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, content=b"{ definitely not json")

    with pytest.raises(NotacError, match="invalid JSON"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert published_path(tmp_path, "aviation", "notams.json") is None


def test_oversized_response_is_rejected(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, content=b"{" + b"x" * MAX_BODY_BYTES + b"}")

    with pytest.raises(NotacError, match="size limit"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))


def test_network_error_does_not_echo_token(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection failed for {TOKEN}", request=request)

    with pytest.raises(NotacError) as error:
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert "connection failed" in str(error.value)
    assert TOKEN not in str(error.value)


def test_repeated_provider_ids_are_rejected_even_when_rows_conflict(tmp_path):
    first = row("A1234/26")
    second = row("A1235/26", "YPJT")
    second["id"] = first["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([first, second], count=2))

    with pytest.raises(NotacError, match="repeated"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))


def test_single_api_record_cannot_contain_multiple_icao_notams(tmp_path):
    record = row("A1234/26")
    record["raw"] = raw("A1234/26") + "\n" + raw("A1235/26")

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([record]))

    with pytest.raises(NotacError, match="exactly one|could not be parsed"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))


def test_replacement_and_cancellation_flags_are_resolved_from_raw_notams(tmp_path):
    old = row("A1000/26")
    old["raw"] = raw("A1000/26")
    replacement = row("A1001/26")
    replacement["raw"] = raw("A1001/26", kind="NOTAMR", replaces="A1000/26")
    cancellation = row("A1002/26")
    cancellation["raw"] = raw("A1002/26", kind="NOTAMC", replaces="A1001/26")

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([old, replacement, cancellation]))

    fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    notices = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["notices"]
    by_id = {notice["id"]: notice for notice in notices}
    assert by_id["A1000/26"]["superseded"] is True
    assert by_id["A1001/26"]["cancelled"] is True


def test_cli_reads_token_from_stdin_and_dry_run_keeps_secret_out_of_output(monkeypatch, capsys, tmp_path):
    import isobar_data.cli as cli

    seen: dict = {}

    def fake_fetch(root, token, **kwargs):
        seen.update(root=root, token=token, kwargs=kwargs)
        return {"ok": True, "published": False}

    monkeypatch.setattr(cli, "fetch_notams", fake_fetch)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(TOKEN + "\n"))
    code = cli.main(["fetch-notams", "--token-stdin", "--dry-run", "--data-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert seen["token"] == TOKEN
    assert seen["kwargs"]["publish"] is False
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_newer_notam_snapshot_published_during_fetch_wins_and_fetch_aborts(tmp_path):
    newer = {"schema_version": 1, "source": "newer", "notices": [{"id": "NEW"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        publish_aviation(tmp_path, {"notams.json": newer})
        return httpx.Response(200, json=response([row("A1234/26")]))

    with pytest.raises(NotacError, match="snapshot changed"):
        fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert json.loads(published_path(tmp_path, "aviation", "notams.json").read_text()) == {
        **newer, "contract": "isobar-data", "family": "aviation",
    }


def test_weather_only_update_during_fetch_is_merged_with_successful_notam_publish(tmp_path):
    old = {"schema_version": 1, "source": "old", "notices": [{"id": "OLD"}]}
    publish_aviation(tmp_path, {"notams.json": old})

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        publish_aviation(tmp_path, {"weather.json": {"updated": True}})
        return httpx.Response(200, json=response([row("A1234/26")]))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    assert result["published"] is True
    assert json.loads(published_path(tmp_path, "aviation", "weather.json").read_text()) == {
        "updated": True, "schema_version": 1, "contract": "isobar-data", "family": "aviation",
    }
    assert json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["notices"][0]["id"] == "A1234/26"


def test_cancellation_without_c_field_is_cancelled_and_not_counted(tmp_path):
    cancellation = row("A1003/26", status="cancelled")
    cancellation["raw"] = """A1003/26 NOTAMC A1000/26
Q) YPPH/QMRLC/IV/NBO/A/000/999/3156S11558E005
A) YPPH
B) 2609270100
E) CANCELLED.
"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([cancellation]))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    notice = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["notices"][0]
    assert notice["cancelled"] is True
    assert notice["effective_to"] is None
    assert result["counts_by_location"]["YPPH"] == 0


def test_ufn_end_is_unknown_and_not_marked_permanent(tmp_path):
    ufn = row("A1004/26")
    ufn["raw"] = raw("A1004/26", end="UFN")

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([ufn]))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    notice = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["notices"][0]
    assert notice["effective_to"] is None
    assert notice["permanent"] is False
    assert result["counts_by_location"]["YPPH"] == 1


def test_expired_known_notice_is_retained_but_excluded_from_counts(tmp_path):
    expired = row("A1005/26", status="expired")
    expired["raw"] = raw("A1005/26", start="2609260100", end="2609262300")

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([expired]))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    notice = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["notices"][0]
    assert notice["effective_to"] == "2026-09-26T23:00:00Z"
    assert result["counts_by_location"]["YPPH"] == 0


def test_elapsed_estimated_end_is_retained_and_counted(tmp_path):
    estimated = row("A1006/26")
    estimated["raw"] = raw("A1006/26", start="2609260100", end="2609262300", estimated=True)

    def handler(request: httpx.Request) -> httpx.Response:
        assert_search_request(request)
        return httpx.Response(200, json=response([estimated]))

    result = fetch_notams(tmp_path, TOKEN, now=NOW, transport=httpx.MockTransport(handler))
    notice = json.loads(published_path(tmp_path, "aviation", "notams.json").read_text())["notices"][0]
    assert notice["estimated"] is True
    assert notice["effective_to"] == "2026-09-26T23:00:00Z"
    assert result["counts_by_location"]["YPPH"] == 1
