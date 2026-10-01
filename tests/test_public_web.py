from __future__ import annotations

from datetime import datetime, timezone

import pytest

from isobar_data.cli import main
from isobar_data.config import load_config
from isobar_data.scheduler import build_sources, run_once


UTC = timezone.utc


def test_public_web_source_profile_is_bounded():
    ids = [source.id for source in build_sources(load_config(), profile="public-web")]
    assert ids == ["open-meteo-ifs", "open-meteo-marine", "ecmwf-open-data"]
    assert "bom-obs" not in ids
    assert not any(source_id.startswith("aviation-") for source_id in ids)


def test_default_source_profile_is_unchanged():
    ids = {source.id for source in build_sources(load_config())}
    assert {"bom-obs", "aviation-metar", "open-meteo-ensemble", "kite"} <= ids


def test_unknown_profile_is_rejected_by_scheduler():
    with pytest.raises(ValueError, match="unknown source profile"):
        build_sources(load_config(), profile="private")


def test_cli_rejects_unknown_profile():
    with pytest.raises(SystemExit):
        main(["--profile", "private"])


def test_run_once_passes_public_web_sources_to_same_scheduler(tmp_path, monkeypatch):
    seen = []

    def fake_run_sources(sources, _ctx):
        seen.extend(source.id for source in sources)
        return []

    monkeypatch.setattr("isobar_data.scheduler.run_sources", fake_run_sources)
    monkeypatch.setattr(
        "isobar_data.scheduler.retain",
        lambda _root, _now: {"removed": [], "within_budget": True},
    )
    run_once(tmp_path, load_config(), datetime(2026, 9, 27, tzinfo=UTC), profile="public-web")
    assert seen == ["open-meteo-ifs", "open-meteo-marine", "ecmwf-open-data"]
