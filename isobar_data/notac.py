"""Bounded NOTAC snapshots; no credentials or generated readings are persisted.

Search is reconciled in full so a withdrawal disappears on the next successful
fetch. We deliberately do not enable scheduled polling until live coverage and
the account's request budget have been measured.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx

from isobar_data.aviation_feed import publish_aviation
from isobar_data.identity import USER_AGENT
from isobar_data.notams import _snapshot_lock, parse_notams
from isobar_data.publish import read_published

BASE = "https://notac.aero/api/v1/notam/"
SOURCE = "NOTAC · unofficial"
DEFAULT_LOCATIONS = ("YPPH", "YPJT", "YMMM")
MAX_BODY_BYTES = 4 * 1024 * 1024


class NotacError(RuntimeError):
    """A safe, credential-free error for the CLI or app."""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_token(token: str) -> str:
    token = token.strip()
    if not re.fullmatch(r"lb_[0-9a-fA-F]{40}", token):
        raise ValueError("NOTAC requires a secret API key (lb_ followed by 40 hex characters)")
    return token


def _record_id(value: Any) -> str:
    if not isinstance(value, str):
        raise NotacError("NOTAC record is missing its provider ID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise NotacError("NOTAC record has an invalid provider ID") from None
    if str(parsed) != value.lower():
        raise NotacError("NOTAC record has an invalid provider ID")
    return str(parsed)


def _next_page(value: Any, query: dict[str, str], page: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NotacError("NOTAC returned invalid pagination")
    # Never forward a bearer token to a URL supplied by the response without
    # checking its origin, path and complete query. Redirects are also disabled.
    try:
        parsed = urlsplit(value)
        expected = {key: [item] for key, item in query.items()}
        expected["page"] = [str(page + 1)]
        safe = (parsed.scheme == "https" and parsed.netloc == "notac.aero"
                and parsed.path == "/api/v1/notam/" and not parsed.fragment
                and parse_qs(parsed.query, keep_blank_values=True) == expected)
    except ValueError:
        safe = False
    if not safe:
        raise NotacError("NOTAC returned an unexpected next-page URL")
    return BASE + "?" + urlencode({**query, "page": str(page + 1)})


class _Session:
    def __init__(self, client: httpx.Client, token: str, max_requests: int):
        self.client = client
        self.token = token
        self.max_requests = max_requests
        self.requests = 0
        self.credits_used = 0

    def get(self, url: str, cost: int) -> dict[str, Any]:
        if self.requests >= self.max_requests:
            raise NotacError("NOTAC request limit reached; previous snapshot retained")
        self.requests += 1
        self.credits_used += cost
        try:
            with self.client.stream("GET", url, headers={"Authorization": f"Bearer {self.token}"}) as response:
                status = response.status_code
                if status != 200:
                    reason = {401: "API key rejected", 402: "monthly credits exhausted",
                              403: "access refused", 429: "rate limit reached"}.get(status, "request failed")
                    raise NotacError(f"NOTAC {reason} (HTTP {status}); no automatic retry")
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise NotacError("NOTAC response exceeds the size limit")
                    body.extend(chunk)
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    raise NotacError("NOTAC returned invalid JSON") from None
        except httpx.HTTPError:
            # Exception text can contain URLs/headers from custom transports.
            raise NotacError("NOTAC connection failed; previous snapshot retained") from None
        if not isinstance(payload, dict):
            raise NotacError("NOTAC returned an invalid response object")
        return payload


def _notice(record: dict[str, Any], locations: tuple[str, ...]) -> dict[str, Any]:
    raw = record.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        raise NotacError("NOTAC detail is missing original source text")
    try:
        notices = parse_notams(raw)
    except ValueError:
        raise NotacError("NOTAC original text could not be parsed; snapshot not published") from None
    if len(notices) != 1:
        raise NotacError("NOTAC record must contain exactly one original notice")
    notice = notices[0]
    if not notice["locations"] or not set(notice["locations"]).intersection(locations):
        raise NotacError("NOTAC returned a notice outside the requested locations")
    if not notice["effective_from"] or not notice["body"]:
        raise NotacError("NOTAC original text is missing its start time or body")
    open_end = bool(re.search(r"(?:^|\s)C\)\s*UFN\b", raw, re.I))
    if notice["kind"] != "NOTAMC" and not notice["effective_to"] and not notice["permanent"] and not notice["estimated"] and not open_end:
        raise NotacError("NOTAC original text is missing its end time")
    if record.get("number") != notice["id"]:
        raise NotacError("NOTAC number conflicts with original text")
    location = record.get("location_code")
    if location is not None and location not in notice["locations"]:
        raise NotacError("NOTAC location conflicts with original text")
    status = record.get("status")
    if status not in {"active", "upcoming", "expired", "cancelled"}:
        raise NotacError("NOTAC returned an unknown notice status")
    if notice["kind"] == "NOTAMC" or status == "cancelled" or record.get("record_archived_at") is not None:
        notice["cancelled"] = True
    notice["provider_id"] = _record_id(record.get("id"))
    notice["provider_status"] = status
    # Keep the original text byte-for-byte after JSON decoding, including its
    # whitespace. Provider-generated 'readings' never enter the product.
    notice["raw"] = raw
    fir = record.get("affected_fir")
    if fir is not None:
        if not isinstance(fir, str) or not re.fullmatch(r"[A-Z]{4}", fir):
            raise NotacError("NOTAC returned an invalid FIR")
        notice["affected_fir"] = fir
    return notice


def fetch_notams(
    root: Path,
    token: str,
    *,
    locations: tuple[str, ...] = DEFAULT_LOCATIONS,
    now: datetime | None = None,
    hours: int = 72,
    max_requests: int = 150,
    transport: httpx.BaseTransport | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Fetch a complete, scoped snapshot or leave the previous snapshot intact.

    ``locations`` is the NOTAM A-field, including FIR-wide notices when a FIR
    code is given. It does not mean every aerodrome inside that FIR. The report's
    ``query_complete`` describes pagination, not authority coverage or a
    transactionally consistent provider snapshot. The search API has no snapshot
    token; count changes and duplicate IDs abort the fetch, but a same-count
    membership swap during paging cannot always be detected.
    """
    token = validate_token(token)
    if not locations or len(locations) > 20 or any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{4}", code) for code in locations):
        raise ValueError("provide 1–20 four-letter uppercase ICAO location codes")
    locations = tuple(dict.fromkeys(locations))
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError("NOTAC forecast window must be 1–168 hours")
    if type(max_requests) is not int or not 1 <= max_requests <= 200:
        raise ValueError("NOTAC request limit must be 1–200")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("NOTAC time must include a timezone")
    previous = read_published(root, "aviation", "notams.json") if publish else None
    query = {"location": ",".join(locations), "status": "any", "sort": "oldest",
             "valid_from": _stamp(moment), "valid_to": _stamp(moment + timedelta(hours=hours))}
    url: str | None = BASE + "?" + urlencode({**query, "page": "1"})
    page, expected_count = 1, None
    records: dict[str, dict[str, Any]] = {}
    notices = []
    with httpx.Client(transport=transport, follow_redirects=False, timeout=30,
                      headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
        session = _Session(client, token, max_requests)
        while url:
            payload = session.get(url, 2)
            count, rows = payload.get("count"), payload.get("results")
            if type(count) is not int or count < 0 or not isinstance(rows, list) or "next" not in payload:
                raise NotacError("NOTAC returned a malformed result page")
            if expected_count is None:
                expected_count = count
            if count != expected_count:
                raise NotacError("NOTAC changed during pagination; snapshot not published")
            next_url = _next_page(payload["next"], query, page)
            if next_url and not rows:
                raise NotacError("NOTAC returned an empty intermediate page")
            for row in rows:
                if not isinstance(row, dict):
                    raise NotacError("NOTAC returned an invalid notice record")
                ident = _record_id(row.get("id"))
                if ident in records:
                    raise NotacError("NOTAC repeated a record during pagination")
                records[ident] = row
            if len(records) > count:
                raise NotacError("NOTAC result count does not match its records")
            url, page = next_url, page + 1
        if len(records) != expected_count:
            raise NotacError("NOTAC snapshot is incomplete; previous snapshot retained")
        for ident, row in records.items():
            if not isinstance(row.get("raw"), str) or not row["raw"].strip():
                detail = session.get(BASE + ident + "/", 1)
                if _record_id(detail.get("id")) != ident:
                    raise NotacError("NOTAC detail returned a different notice")
                # Do not combine a new detail with an older search row.
                for key in ("number", "location_code", "status", "effective_start", "effective_end"):
                    if key in row and row[key] != detail.get(key):
                        raise NotacError("NOTAC notice changed during fetch; snapshot not published")
                row = detail
            notices.append(_notice(row, locations))
    # Resolve replacements across the snapshot without losing provider fields.
    resolved = parse_notams([notice["raw"] for notice in notices])
    flags = {notice["raw"].strip(): notice for notice in resolved}
    for notice in notices:
        for flag in ("cancelled", "superseded"):
            if flags[notice["raw"].strip()].get(flag):
                notice[flag] = True
    current = [notice for notice in notices if not notice.get("cancelled") and not notice.get("superseded")
               and (notice["estimated"] or not notice["effective_to"]
                    or datetime.fromisoformat(notice["effective_to"].replace("Z", "+00:00")) > moment)]
    counts = {code: sum(code in notice["locations"] for notice in current) for code in locations}
    coverage = {"locations": list(locations), "valid_from": query["valid_from"], "valid_to": query["valid_to"]}
    product = {"schema_version": 1, "source": SOURCE, "source_url": "https://notac.aero/",
               "licence_id": "notac-terms", "attribution": [SOURCE],
               "provider": "NOTAC", "retrieved_at": _stamp(moment if now else datetime.now(timezone.utc)),
               "imported": False, "coverage": coverage, "coverage_basis": "provider_query", "notices": notices}
    if publish:
        with _snapshot_lock(root):
            if read_published(root, "aviation", "notams.json") != previous:
                raise NotacError("NOTAM snapshot changed during fetch; newer snapshot retained")
            publish_aviation(root, {"notams.json": product})
    return {"ok": True, "query_complete": True, "published": publish, "source": SOURCE,
            "count": len(notices), "counts_by_location": counts, "coverage": coverage,
            "requests": session.requests, "credits_used": session.credits_used,
            "retrieved_at": product["retrieved_at"]}
