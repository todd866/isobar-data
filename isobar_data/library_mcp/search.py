"""Call LocalEvidence /api/ask and keep passages from the aviation collection."""
from __future__ import annotations

import json
import ipaddress
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_COLLECTION = Path.home() / "Projects" / "PaperLibrary" / "collections" / "aviation-meteorology.jsonl"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _local_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("library URL must be an unauthenticated http loopback URL")
    host = parsed.hostname.rstrip(".").lower()
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("library URL must use a loopback host")
        except ValueError as exc:
            raise ValueError("library URL must use a loopback host") from exc
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("library URL has an invalid port") from exc
    if parsed.query or parsed.fragment:
        raise ValueError("library URL must not include a query or fragment")
    return value.rstrip("/")


def load_collection(path: Path | None = None) -> list[dict]:
    """Read the aviation-meteorology slug list. Missing file is an empty allowlist."""
    source = Path(path or os.environ.get("ISOBAR_LIBRARY_COLLECTION") or DEFAULT_COLLECTION)
    if not source.is_file():
        return []
    rows = []
    for line in source.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def library_search(
    query: str,
    k: int = 5,
    *,
    base_url: str | None = None,
    collection: list[dict] | None = None,
    timeout: float | None = None,
) -> dict:
    """Return up to ``k`` aviation passages from the retrieval-only ask endpoint.

    The endpoint is the same one md3's tutor calls. It is not filtered server-side,
    so passages whose slug is outside the aviation collection are dropped. Licence
    and source URL come from that collection. Page and section are passed through
    when the endpoint sends them.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer")
    records = collection if collection is not None else load_collection()
    by_slug = {str(row.get("slug")): row for row in records if isinstance(row, dict) and row.get("slug")}
    if not by_slug:
        return {"passages": [], "reason": "aviation collection is empty"}
    url = _local_http_url(base_url or os.environ.get("ISOBAR_LIBRARY_URL") or DEFAULT_URL)
    if timeout is None:
        timeout = float(os.environ.get("ISOBAR_LIBRARY_TIMEOUT_MS", "45000")) / 1000
    request = urllib.request.Request(
        f"{url}/api/ask",
        data=json.dumps({"question": query.strip()}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler)
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > MAX_RESPONSE_BYTES:
                        return {"passages": [], "reason": "library response exceeded the size limit"}
                except ValueError:
                    pass
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return {"passages": [], "reason": "library response exceeded the size limit"}
            payload = json.loads(body.decode())
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return {"passages": [], "reason": "library redirects are not allowed"}
        return {"passages": [], "reason": f"library returned {exc.code}"}
    except (urllib.error.URLError, TimeoutError, UnicodeError, json.JSONDecodeError, OSError) as exc:
        return {"passages": [], "reason": str(exc)}
    if 300 <= status < 400:
        return {"passages": [], "reason": "library redirects are not allowed"}
    if status >= 400:
        return {"passages": [], "reason": f"library returned {status}"}
    if not isinstance(payload, dict):
        return {"passages": [], "reason": "library returned a response that was not an object"}
    evidence = payload.get("evidence")
    if payload.get("status") == "answered" and not evidence:
        return {"passages": [], "reason": "library returned a prior answer, not aviation passages"}
    passages = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        record = by_slug.get(str(item.get("slug") or ""))
        if record is None:
            continue
        passage = item.get("text") if item.get("text") is not None else (item.get("passage") or "")
        if not isinstance(passage, str) or not passage.strip():
            continue
        source = record.get("source_url") or item.get("source") or ""
        item_source = item.get("source_url") or item.get("source")
        passages.append({
            "title": item.get("title") or record.get("title") or "",
            "source": source,
            "page": item.get("page") if item.get("page") not in ("", None) else None,
            "section": item.get("section") or item.get("heading") or None,
            "licence": record.get("licence") or "",
            "passage": passage,
            "provenance": "source-matched-excerpt" if item_source and item_source == record.get("source_url") else "unverified-excerpt",
        })
        if len(passages) >= k:
            break
    return {"passages": passages, "reason": None}
