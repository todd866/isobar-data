"""Version markers for files shared with the Isobar app.

The archive predates an explicit contract marker. Readers still accept legacy
files without these fields, while newly written pointers and manifests
identify the contract they implement.
"""

from __future__ import annotations

CONTRACT = "isobar-data"
SCHEMA_VERSION = 1
# The Mac app accepts schema 1 as 33 frames, 0–96 h, every 3 h. Schema 2
# lists forecast_hours and does not imply a constant step.
GRID_SCHEMA_VERSION = 2
GRID_FAMILY = "grids/ecmwf_ifs025"
GLOBAL_GRID_FAMILY = "grids/ecmwf_ifs_global"


def accepted_schema_versions(family: str | None) -> frozenset[int]:
    """Versions this collector will read back.

    A grid pointer from before the explicit hour list is still readable so
    the next complete run can replace it. Any newer grid schema is refused.
    """
    if family in {GRID_FAMILY, GLOBAL_GRID_FAMILY}:
        return frozenset({SCHEMA_VERSION, GRID_SCHEMA_VERSION})
    return frozenset({SCHEMA_VERSION})


def marker(*, family: str | None = None) -> dict[str, object]:
    """Return the stable, additive marker used by generated archive files."""
    value: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
    }
    if family:
        value["family"] = family
    return value
