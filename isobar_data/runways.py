"""OurAirports runway headings. The world CSV is not kept after the filter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from isobar_data.aviation_feed import publish_aviation
from isobar_data.http import Http
from isobar_data.normalise import filter_runways

URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"


def fetch_runways(http: Http, root, state: dict, icaos: list[str], now: datetime) -> dict:
    key = "ourairports-runways"
    validators = (state.get("validators") or {}).get(key) or {}
    stored_icaos = (state.get("sources") or {}).get(key, {}).get("icaos")
    icao_key = ",".join(icaos)
    changed_list = stored_icaos != icao_key
    last = (state.get("sources") or {}).get(key, {}).get("checked")
    if last and not changed_list:
        checked = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if now < checked + timedelta(days=7) and validators.get("modified"):
            head = http.head(
                URL,
                bucket="ourairports",
                weight=1,
                modified=validators.get("modified"),
                accept="text/csv",
                timeout=30,
            )
            if head.status_code == 304:
                return {"ok": True, "complete": True, "detail": "not modified", "run": validators.get("modified")}
            if head.status_code == 200 and head.headers.get("last-modified") == validators.get("modified"):
                return {"ok": True, "complete": True, "detail": "not modified", "run": validators.get("modified")}
    response = http.get(URL, bucket="ourairports", weight=1, accept="text/csv", timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"runways HTTP {response.status_code}")
    text = response.content.decode("utf-8", "replace")
    product = filter_runways(text, set(icaos))
    modified = response.headers.get("last-modified")
    product["source_last_modified"] = modified
    product["fetched"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    publish_aviation(root, {"runways.json": product})
    state.setdefault("validators", {})[key] = {"modified": modified, "etag": response.headers.get("etag")}
    state.setdefault("sources", {}).setdefault(key, {})["icaos"] = icao_key
    state["sources"][key]["checked"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "ok": True,
        "complete": True,
        "detail": f"{len(product['runways'])} runways",
        "run": modified,
        "count": len(product["runways"]),
    }
