import json
import fcntl
from datetime import datetime, timezone
from pathlib import Path

import pytest

from isobar_data.notams import import_notams, parse_notams
from isobar_data.aviation_feed import publish_aviation
from isobar_data.publish import published_path


PLAIN = """A1234/26 NOTAMN
Q) YPPH/QMRLC/IV/NBO/A/000/999/3156S11558E005
A) YPPH
B) 2609270100
C) 2609270600
D) DAILY 0100-0600
E) RWY 03/21 CLOSED FOR WORKS. WILDCAT ACTIVITY CONTINUES.
F) SFC
G) 999

A1235/26 NOTAMR A1234/26
Q) YPPH/QICAS/IV/NBO/A/000/999/3156S11558E005
A) YPPH
B) 2609270200
C) PERM
E) ILS RWY 21 OUT OF SERVICE.
"""


def test_plaintext_preserves_raw_and_extracts_fields():
    notices = parse_notams(PLAIN)
    assert [n["id"] for n in notices] == ["A1234/26", "A1235/26"]
    first, second = notices
    assert first["kind"] == "NOTAMN"
    assert first["locations"] == ["YPPH"]
    assert first["effective_from"] == "2026-09-27T01:00:00Z"
    assert first["effective_to"] == "2026-09-27T06:00:00Z"
    assert first["schedule"] == "DAILY 0100-0600"
    assert first["lower"] == "SFC" and first["upper"] == "999"
    assert first["category"] == "runway"
    assert first["title"] == "Runway 03/21 CLOSED FOR WORKS. WILDCAT ACTIVITY CONTINUES."
    assert first["raw"].startswith("A1234/26 NOTAMN")
    assert first["superseded"] is True
    assert second["permanent"] is True
    assert second["replaces"] == "A1234/26"
    assert second["category"] == "approach"


def test_inline_parenthesized_fields_and_standard_predecessor_id():
    notices = parse_notams("""(D1234/26 NOTAMC D1200/26 A) YPPH B) 2609270100 C) 2609270200 E) RWY CLSD.)""")
    assert len(notices) == 1
    assert notices[0]["id"] == "D1234/26"
    assert notices[0]["replaces"] == "D1200/26"
    assert notices[0]["effective_from"] == "2026-09-27T01:00:00Z"
    assert notices[0]["title"] == "Runway closed."


def test_notamn_prose_replaces_does_not_resolve_and_missing_location_is_ambiguous():
    notices = parse_notams("""A1111/26 NOTAMN
A) YPPH
B) 2609270100
C) 2609270200
E) REPLACES A0001/26.

A0001/26 NOTAMN
A) YPPH
B) 2609260100
C) 2609260200
E) OLD.

A0002/26 NOTAMC A0001/26
B) 2609270100
C) 2609270200
E) CANCELLED.
""")
    assert not notices[1].get("cancelled")
    assert not notices[1].get("superseded")


def test_geometry_is_safe_and_unknown_dates_stay_unknown():
    notices = parse_notams("""B1234/26 NOTAMN
Q) YPPH/QOBCE/IV/M/AE/000/999/3156S11558E005
A) YPPH
B) 2609270100
C) EST
E) CRANE ERECTED.
""")
    notice = notices[0]
    assert notice["latitude"] == pytest.approx(-31.9333, abs=0.001)
    assert notice["longitude"] == pytest.approx(115.9667, abs=0.001)
    assert notice["radius_nm"] == 5
    assert notice["effective_to"] is None
    assert notice["estimated"] is True


def test_json_canonical_envelope_keeps_supplied_metadata():
    payload = {"schema_version": 1, "source": "fixture", "notices": [{
        "id": "C1234/26", "kind": "NOTAMC", "replaces": "A1234/26", "locations": ["YPPH"],
        "category": "runway", "title": "RWY CLOSED.", "raw": "C1234/26 NOTAMC\nE) RWY CLOSED.",
        "effective_from": None, "effective_to": None, "permanent": False, "estimated": False,
        "schedule": "", "body": "RWY CLOSED.", "lower": "SFC", "upper": "999",
    }]}
    notice = parse_notams(json.dumps(payload))[0]
    assert notice["kind"] == "NOTAMC"
    assert notice["replaces"] == "A1234/26"
    assert notice["raw"].startswith("C1234/26")


def test_import_publishes_schema_and_fails_empty(tmp_path: Path):
    source = tmp_path / "briefing.txt"
    source.write_text(PLAIN)
    moment = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)
    result = import_notams(source, "synthetic briefing", tmp_path / "data", retrieved_at=moment)
    assert result["count"] == 2
    path = published_path(tmp_path / "data", "aviation", "notams.json")
    assert path is not None
    product = json.loads(path.read_text())
    assert product["schema_version"] == 1
    assert product["source"] == "synthetic briefing"
    assert product["retrieved_at"] == "2026-09-27T00:00:00Z"
    assert product["imported"] is True
    assert len(product["notices"]) == 2
    publish_aviation(tmp_path / "data", {"runways.json": {"schema_version": 1, "runways": []}})
    before = json.loads(published_path(tmp_path / "data", "aviation", "notams.json").read_text())
    empty = tmp_path / "empty.txt"
    empty.write_text("not a NOTAM")
    with pytest.raises(ValueError, match="no valid NOTAM"):
        import_notams(empty, "empty", tmp_path / "data")
    assert json.loads(published_path(tmp_path / "data", "aviation", "notams.json").read_text()) == before
    assert json.loads(published_path(tmp_path / "data", "aviation", "runways.json").read_text()) == {
        "schema_version": 1,
        "contract": "isobar-data",
        "family": "aviation",
        "runways": [],
    }


def test_aviation_update_marks_legacy_products_in_the_new_snapshot(tmp_path: Path):
    from isobar_data.publish import publish_run, run_dir

    root = tmp_path / "data"
    old = run_dir(root, "aviation", "old")
    old.mkdir(parents=True)
    (old / "runways.json").write_text(json.dumps({"runways": []}))
    publish_run(root, "aviation", "old", ["runways.json"])

    publish_aviation(root, {"notams.json": {"notices": []}})
    for name in ("runways.json", "notams.json"):
        product = json.loads(published_path(root, "aviation", name).read_text())
        assert product["schema_version"] == 1
        assert product["contract"] == "isobar-data"
        assert product["family"] == "aviation"


def test_partial_bad_block_is_not_silently_dropped():
    parsed = parse_notams(PLAIN + "\nBROKEN BLOCK\n")
    assert len(parsed) == 2


def test_partial_bad_json_fails_before_publish():
    with pytest.raises(ValueError, match="invalid NOTAM"):
        parse_notams(json.dumps({"notices": [{"id": "A1234/26", "kind": "NOTAMN", "raw": ""}]}))


def test_lighting_wins_over_runway_and_bad_geometry_is_ignored():
    notices = parse_notams("""E1234/26 NOTAMN
Q) YPPH/QXXXX/IV/BO/A/000/999/3199S11599E005
A) YPPH
B) 2609270100
C) 2609270200
E) RWY EDGE LGT U/S.
""")
    assert notices[0]["category"] == "lighting"
    assert "latitude" not in notices[0]
    assert "radius_nm" not in notices[0]


def test_title_keeps_later_qualifiers():
    notice = parse_notams("""F1234/26 NOTAMN
A) YPPH
B) 2609270100
C) 2609270200
E) RWY CLSD. EXCEPT EMERGENCY LANDINGS.
""")[0]
    assert "EXCEPT EMERGENCY LANDINGS" in notice["title"]


def test_existing_scheduler_lock_blocks_import(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    handle = (root / ".lock").open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        source = tmp_path / "briefing.txt"
        source.write_text(PLAIN)
        with pytest.raises(RuntimeError, match="busy"):
            import_notams(source, "locked", root)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@pytest.mark.parametrize("key,value", [
    ("effective_from", 20260927), ("effective_from", "2026-09-27T01:00:00"),
    ("locations", ["lower"]), ("locations", [None]), ("title", []),
    ("raw", ["invalid"]), ("schedule", False), ("permanent", "false"),
    ("id", "Z9999/26"), ("radius_nm", float("nan")),
])
def test_invalid_or_conflicting_canonical_fields_rejected(key, value):
    notice = parse_notams(PLAIN)[0]
    notice[key] = value
    with pytest.raises(ValueError):
        parse_notams({"notices": [notice]})


def test_reversed_dates_rejected_and_offsets_compared_as_instants():
    with pytest.raises(ValueError, match="reversed"):
        parse_notams("A1234/26 NOTAMN A) YPPH B) 2609280100 C) 2609270100 E) RWY CLSD.")
    notice = parse_notams("A1234/26 NOTAMN A) YPPH E) RWY CLSD.")[0]
    notice.update(effective_from="2026-09-27T10:00:00+10:00", effective_to="2026-09-27T01:00:00Z")
    assert len(parse_notams({"notices": [notice]})) == 1
