"""Import NOTAM text or canonical JSON into the aviation snapshot.

The importer deliberately keeps the source wording.  It extracts fields useful
for a compact viewer, but does not attempt to turn a NOTAM into a go/no-go
judgement.
"""

from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from isobar_data.aviation_feed import publish_aviation

_ID = re.compile(r"\b([A-Z][0-9]{4}/[0-9]{2})\b")
_KIND = re.compile(r"\b(NOTAM[NRC])\b")
_FIELD = re.compile(r"(?ms)(?:^|[\s(])([A-GQ])\)\s*(.*?)(?=(?:[\s(][A-GQ]\)\s)|\Z)")
_DATE = re.compile(r"(?<!\d)(\d{10})(?!\d)")
_LEVEL = re.compile(r"\b(?:SFC|FL)?[0-9]{2,3}(?:FT)?\b", re.I)


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _field(raw: str, letter: str) -> str:
    for match in _FIELD.finditer(raw):
        if match.group(1) == letter:
            return re.sub(r"\s+", " ", match.group(2).strip())
    return ""


def _date(value: str) -> str | None:
    match = _DATE.search(value)
    if not match:
        return None
    text = match.group(1)
    try:
        date = datetime.strptime(text, "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return date.strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(raw: str) -> str:
    return _field(raw, "E")


def _title(body: str) -> str:
    body = re.sub(r"\s+", " ", body).strip().rstrip(")").rstrip()
    if not body:
        return "NOTAM"
    title = body
    for pattern, replacement in ((r"\bRWY\b", "Runway"), (r"\bTWY\b", "Taxiway"),
                                 (r"\bCLSD\b", "closed"), (r"\bU/S\b", "unserviceable"),
                                 (r"\bUNSERVICEABLE\b", "unserviceable")):
        title = re.sub(pattern, replacement, title, flags=re.I)
    return title


def _locations(value: str | list[Any]) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = _clean(value)
    if not text:
        return []
    return [part for part in re.split(r"\s*(?:,|/)\s*|\s+", text) if part]


def _category(body: str, qline: str) -> str:
    text = f"{body} {qline}".upper()
    if re.search(r"\b(?:LGT|LIGHT(?:ING)?|PAPI|REIL|BEACON|ALS|STROBE)\b", text):
        return "lighting"
    for category, pattern in (
        ("approach", r"\b(?:ILS|LOC|LOCALIZER|GLIDESLOPE|APPROACH|RNAV|RNP)\b"),
        ("runway", r"\b(?:RWY|RUNWAY|TAXIWAY|TWY)\b"),
        ("navaid", r"\b(?:VOR|VORTAC|NDB|DME|NAVAID|DVOR|GNSS)\b"),
        ("lighting", r"\b(?:PAPI|REIL|LIGHT(?:ING)?|BEACON|ALS|STROBE)\b"),
        ("airspace", r"\b(?:AIRSPACE|FIR|TFR|PROHIBITED|RESTRICTED|DANGER|MILITARY|UAV|DRONE)\b"),
        ("obstacle", r"\b(?:OBSTACLE|CRANE|TOWER|BALLOON|WIRE|STACK)\b"),
        ("services", r"\b(?:FUEL|ATIS|RADIO|SERVICE|CUSTOMS|FIRE|RESCUE|RUNWAY\s+VISUAL)\b"),
    ):
        if re.search(pattern, text):
            return category
    return "other"


def _geometry(qline: str) -> dict[str, float]:
    # Q) .../DDMMNDDDMME/NNN.  Only accept complete, bounded coordinates.
    result: dict[str, float] = {}
    parts = [part.strip() for part in qline.split("/")]
    for part in parts:
        match = re.fullmatch(r"(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])(?:([0-9]{1,3}))?", part.upper())
        if not match:
            continue
        if int(match.group(2)) >= 60 or int(match.group(5)) >= 60:
            continue
        lat = int(match.group(1)) + int(match.group(2)) / 60
        lon = int(match.group(4)) + int(match.group(5)) / 60
        if match.group(3) == "S":
            lat = -lat
        if match.group(6) == "W":
            lon = -lon
        if abs(lat) <= 90 and abs(lon) <= 180:
            result["latitude"] = lat
            result["longitude"] = lon
            if match.group(7) is not None:
                result["radius_nm"] = float(match.group(7))
            break
    return result


def _parse_raw(raw: str, supplied: dict[str, Any] | None = None) -> dict[str, Any] | None:
    supplied = supplied or {}
    raw = raw.strip()
    ident_match = _ID.search(raw)
    kind_match = _KIND.search(raw)
    if not ident_match or not kind_match:
        return None
    ident = ident_match.group(1)
    kind = kind_match.group(1)
    body = _body(raw)
    qline = _field(raw, "Q")
    locations = _locations(_field(raw, "A"))
    start = _date(_field(raw, "B"))
    end_text = _field(raw, "C")
    permanent = bool(re.search(r"\bPERM\b", end_text, re.I))
    estimated = bool(re.search(r"\bEST\b", end_text, re.I))
    end = None if permanent else _date(end_text)
    replaces_match = None
    if kind != "NOTAMN":
        after_kind = raw[kind_match.end() : kind_match.end() + 40]
        replaces_match = re.match(r"\s*(?:REPLACES?|CANCELS?)?\s*([A-Z][0-9]{4}/[0-9]{2})\b", after_kind, re.I)
        if not replaces_match:
            replaces_match = re.search(r"\b(?:REPLACES?|CANCELS?)\s+([A-Z][0-9]{4}/[0-9]{2})\b", raw, re.I)
    replaces = replaces_match.group(1).upper() if replaces_match else None
    result: dict[str, Any] = {
        "id": ident,
        "kind": kind,
        "replaces": replaces,
        "locations": locations,
        "category": _category(body, qline),
        "title": _title(body),
        "raw": raw,
        "effective_from": start,
        "effective_to": end,
        "permanent": permanent,
        "estimated": estimated,
        "schedule": _field(raw, "D"),
        "body": body,
        "lower": _field(raw, "F"),
        "upper": _field(raw, "G"),
    }
    result.update(_geometry(qline))
    if supplied:
        # Validate every supplied value even if the raw parse takes precedence.
        _validate_notice({**result, **supplied})
        for key in ("id", "kind", "permanent", "estimated"):
            if key in supplied and supplied[key] != result[key]:
                raise ValueError(f"canonical NOTAM {key} conflicts with raw source")
    # Canonical JSON may carry fields unavailable in the raw source. Keep the
    # raw parse authoritative for dates and wording, but preserve safe metadata.
    if not result["effective_from"] and supplied.get("effective_from"):
        result["effective_from"] = supplied["effective_from"]
    if not result["effective_to"] and supplied.get("effective_to") and not permanent:
        result["effective_to"] = supplied["effective_to"]
    for key in ("replaces", "locations", "schedule", "lower", "upper", "latitude", "longitude", "radius_nm"):
        if key in supplied and supplied[key] not in (None, "", []):
            if result.get(key) not in (None, "", []) and result.get(key) != supplied[key]:
                raise ValueError(f"canonical NOTAM {key} conflicts with raw source")
            if result.get(key) in (None, "", []):
                result[key] = supplied[key]
    for key in ("effective_from", "effective_to"):
        if supplied.get(key) is not None and result.get(key) is not None and result[key] != supplied[key]:
            raise ValueError(f"canonical NOTAM {key} conflicts with raw source")
    return result


def _raw_blocks(text: str) -> list[str]:
    starts = list(re.finditer(r"(?m)^\s*\(?[A-Z][0-9]{4}/[0-9]{2}\s+NOTAM[NRC]\b", text))
    if not starts:
        return [text.strip()] if text.strip() else []
    return [text[m.start() : starts[i + 1].start() if i + 1 < len(starts) else len(text)].strip() for i, m in enumerate(starts)]


def _validate_notice(notice: dict[str, Any]) -> None:
    if not isinstance(notice.get("raw"), str) or not notice["raw"].strip():
        raise ValueError("NOTAM record is missing raw source text")
    if not re.fullmatch(r"[A-Z][0-9]{4}/[0-9]{2}", str(notice.get("id", ""))):
        raise ValueError("NOTAM record has invalid id")
    if notice.get("kind") not in {"NOTAMN", "NOTAMR", "NOTAMC"}:
        raise ValueError("NOTAM record has invalid kind")
    if not isinstance(notice.get("locations"), list) or any(not isinstance(item, str) or not re.fullmatch(r"[A-Z]{4}", item) for item in notice["locations"]):
        raise ValueError("NOTAM locations must be a list of strings")
    for key in ("effective_from", "effective_to"):
        value = notice.get(key)
        if value is not None:
            if not isinstance(value, str) or not re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", value):
                raise ValueError(f"invalid NOTAM {key}")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"invalid NOTAM {key}") from exc
    if notice.get("effective_from") and notice.get("effective_to") and datetime.fromisoformat(notice["effective_to"].replace("Z", "+00:00")) < datetime.fromisoformat(notice["effective_from"].replace("Z", "+00:00")):
        raise ValueError("NOTAM effective dates are reversed")
    for key in ("category", "title", "schedule", "body", "lower", "upper"):
        if not isinstance(notice.get(key), str):
            raise ValueError(f"invalid NOTAM {key}")
    if notice.get("replaces") is not None and not re.fullmatch(r"[A-Z][0-9]{4}/[0-9]{2}", str(notice["replaces"])):
        raise ValueError("invalid NOTAM replaces")
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180), ("radius_nm", 0, 999)):
        value = notice.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high):
            raise ValueError(f"invalid NOTAM {key}")
    for key in ("permanent", "estimated"):
        if not isinstance(notice.get(key), bool):
            raise ValueError(f"invalid NOTAM {key}")


def _resolve(notices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for notice in notices:
        raw = notice["raw"]
        if raw in seen:
            continue
        seen.add(raw)
        unique.append(notice)
    for notice in unique:
        replacement = notice.get("replaces")
        if not replacement:
            continue
        if notice.get("kind") not in {"NOTAMR", "NOTAMC"} or not notice.get("locations"):
            continue
        candidates = [old for old in unique if old is not notice and old.get("id") == replacement and old.get("locations") == notice.get("locations")]
        if len(candidates) == 1:
            candidates[0]["cancelled" if notice["kind"] == "NOTAMC" else "superseded"] = True
    return unique


def parse_notams(payload: str | bytes | dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Parse a plaintext briefing or canonical JSON envelope."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            notices = [notice for raw in _raw_blocks(payload) if (notice := _parse_raw(raw))]
            for notice in notices:
                _validate_notice(notice)
            return _resolve(notices)
        payload = decoded
    records: list[Any]
    if isinstance(payload, dict):
        records = payload.get("notices") if isinstance(payload.get("notices"), list) else [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        return []
    notices: list[dict[str, Any]] = []
    invalid = 0
    for record in records:
        if isinstance(record, str):
            parsed = _parse_raw(record)
        elif isinstance(record, dict):
            parsed = _parse_raw(record["raw"], record) if isinstance(record.get("raw"), str) and record["raw"].strip() else None
            if parsed is None and record.get("id") and record.get("kind") and record.get("raw"):
                parsed = {key: record.get(key) for key in ("id", "kind", "replaces", "locations", "category", "title", "raw", "effective_from", "effective_to", "permanent", "estimated", "schedule", "body", "lower", "upper")}
                parsed.update({key: record[key] for key in ("latitude", "longitude", "radius_nm") if key in record})
        else:
            parsed = None
        if parsed and parsed.get("id") and parsed.get("kind"):
            _validate_notice(parsed)
            notices.append(parsed)
        elif isinstance(payload, (dict, list)):
            invalid += 1
    if invalid:
        raise ValueError(f"{invalid} invalid NOTAM record(s) in input")
    return _resolve(notices)


@contextmanager
def _snapshot_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".lock").open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"data directory is busy: {root}") from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def import_notams(path: Path, source: str, root: Path, *, retrieved_at: datetime | None = None) -> dict[str, Any]:
    """Parse and publish one NOTAM source, failing visibly when none are valid."""
    data = path.read_bytes()
    notices = parse_notams(data)
    if not notices:
        raise ValueError(f"no valid NOTAM records found in {path}")
    retrieved = (retrieved_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    product = {"schema_version": 1, "source": source, "retrieved_at": retrieved, "imported": True, "notices": notices}
    with _snapshot_lock(root):
        publish_aviation(root, {"notams.json": product})
    return {"ok": True, "count": len(notices), "source": source, "path": str(root / "products" / "aviation" / "notams.json")}
