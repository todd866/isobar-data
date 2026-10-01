"""Stage a run, then swap current.json in one rename.

Readers follow that pointer. A run with a missing artifact is not published.
A published run directory is never opened for write: a new generation is a
new directory, and the pointer moves only after that directory is complete.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from isobar_data.contract import CONTRACT, SCHEMA_VERSION, marker
from isobar_data.storage import atomic_write


class IncompleteRun(RuntimeError):
    """Required artifacts are missing. The current pointer is unchanged."""


class UnsupportedContract(RuntimeError):
    """A newer or mismatched archive cannot be safely republished."""


def run_dir(root: Path, family: str, run_id: str) -> Path:
    return root / "products" / family / "runs" / run_id


def pointer_path(root: Path, family: str) -> Path:
    return root / "products" / family / "current.json"


def read_pointer(root: Path, family: str) -> dict:
    path = pointer_path(root, family)
    if not path.is_file():
        return {"latest": None, "runs": []}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"latest": None, "runs": []}
    if not isinstance(payload, dict):
        return {"latest": None, "runs": []}
    if ("schema_version" in payload and (type(payload["schema_version"]) is not int or
                                         payload["schema_version"] != SCHEMA_VERSION)) or (
        "contract" in payload and payload["contract"] != CONTRACT
    ) or ("family" in payload and payload["family"] != family):
        raise UnsupportedContract(f"unsupported {family} pointer at {path}")
    runs = []
    for item in payload.get("runs") or []:
        if item and str(item) not in runs:
            runs.append(str(item))
    latest = payload.get("latest")
    return {"latest": None if latest is None else str(latest), "runs": runs}


def publish_run(
    root: Path,
    family: str,
    run_id: str,
    required: list[str],
    *,
    keep: str = "latest",
) -> None:
    """Publish ``run_id`` only when every required relative path exists."""
    directory = run_dir(root, family, run_id)
    missing = [rel for rel in required if not (directory / rel).is_file()]
    if missing:
        raise IncompleteRun(
            f"{family} {run_id} missing {len(missing)} artifacts, including {missing[0]}"
        )
    current = read_pointer(root, family)
    if keep == "all":
        runs = [item for item in current["runs"] if item != run_id]
        runs.append(run_id)
        runs.sort()
        # Run ids are UTC stamps (20260926T00Z). The last published cycle can be
        # the older of the pair; readers follow the newest one.
        latest = runs[-1]
    elif keep == "previous":
        prior = current.get("latest")
        runs = [prior] if prior and prior != run_id else []
        runs.append(run_id)
        latest = run_id
    else:
        runs = [run_id]
        latest = run_id
    payload = json.dumps({**marker(family=family), "latest": latest, "runs": runs}, indent=2).encode() + b"\n"
    atomic_write(pointer_path(root, family), payload)


def published_path(root: Path, family: str, name: str) -> Path | None:
    """The file readers should open: the latest run, or the legacy path."""
    latest = read_pointer(root, family).get("latest")
    if latest:
        path = run_dir(root, family, latest) / name
        if path.is_file():
            return path
    legacy = root / "products" / family / name
    if legacy.is_file():
        return legacy
    return None


def commit_files(
    root: Path,
    family: str,
    files: dict[str, bytes],
    *,
    keep: str = "latest",
    write=None,
) -> None:
    """Publish ``files`` without writing into a directory the pointer already names.

    ``write(stage)`` may place the same bytes. It runs only against a staging
    directory. The pointer moves after the staged tree is complete.
    """
    if not files:
        return
    if _matches(_published_directory(root, family), files):
        return
    run_id = _content_token(files)
    pointer = read_pointer(root, family)
    final = run_dir(root, family, run_id)
    if run_id == pointer.get("latest") or run_id in pointer["runs"]:
        if _matches(final, files):
            if pointer.get("latest") != run_id:
                publish_run(root, family, run_id, sorted(files), keep=keep)
            return
        raise IncompleteRun(f"refusing to modify published {family} run {run_id}")
    if final.is_dir() and _matches(final, files):
        publish_run(root, family, run_id, sorted(files), keep=keep)
        return
    staging = root / "staging" / family / run_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        if write is None:
            for name, data in files.items():
                _check_name(name)
                atomic_write(staging / name, data)
        else:
            write(staging)
        missing = [name for name, data in files.items() if not _same(staging / name, data)]
        if missing:
            raise IncompleteRun(f"{family} {run_id} missing {missing[0]}")
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            discard = final.with_name(final.name + ".discard")
            if discard.exists():
                shutil.rmtree(discard)
            final.rename(discard)
            try:
                staging.rename(final)
            except Exception:
                if not final.exists() and discard.exists():
                    discard.rename(final)
                raise
            shutil.rmtree(discard, ignore_errors=True)
        else:
            staging.rename(final)
        publish_run(root, family, run_id, sorted(files), keep=keep)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _published_directory(root: Path, family: str) -> Path | None:
    latest = read_pointer(root, family).get("latest")
    if latest:
        directory = run_dir(root, family, latest)
        return directory if directory.is_dir() else None
    legacy = root / "products" / family
    return legacy if legacy.is_dir() else None


def _matches(directory: Path | None, files: dict[str, bytes]) -> bool:
    if directory is None or not directory.is_dir():
        return False
    suffixes = {Path(name).suffix.lower() for name in files}
    present: dict[str, bytes] = {}
    for path in directory.iterdir():
        if not path.is_file() or path.name == "current.json":
            continue
        if path.suffix.lower() not in suffixes:
            continue
        present[path.name] = path.read_bytes()
    return present == files


def _same(path: Path, data: bytes) -> bool:
    return path.is_file() and path.read_bytes() == data


def _content_token(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        _check_name(name)
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[name]).digest())
    return digest.hexdigest()[:20]


def _check_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in {".", ".."} or name == "current.json":
        raise IncompleteRun(f"refusing product name {name!r}")


def read_published(root: Path, family: str, name: str) -> dict | None:
    """The latest published JSON, or a legacy file when nothing has been published."""
    latest = read_pointer(root, family).get("latest")
    if latest:
        path = run_dir(root, family, latest) / name
    else:
        path = root / "products" / family / name
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None
