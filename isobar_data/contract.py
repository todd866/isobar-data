"""Version markers for files shared with the Isobar app.

The archive predates an explicit contract marker. Readers still accept legacy
files without these fields, while newly written pointers and manifests
identify the contract they implement.
"""

from __future__ import annotations

CONTRACT = "isobar-data"
SCHEMA_VERSION = 1


def marker(*, family: str | None = None) -> dict[str, object]:
    """Return the stable, additive marker used by generated archive files."""
    value: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
    }
    if family:
        value["family"] = family
    return value
