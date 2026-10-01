"""Temp-file renames, content hashes, and zstd for bulky text."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import zstandard as zstd

COMPRESS_OVER = 32 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def write_if_changed(path: Path, data: bytes) -> bool:
    """Write only when the bytes differ. Returns True when the file was replaced."""
    if path.is_file() and sha256(path.read_bytes()) == sha256(data):
        return False
    atomic_write(path, data)
    return True


def store_raw(path: Path, data: bytes, *, compressible: bool) -> Path:
    """Keep Bureau tarballs and GRIB as fetched. Compress large JSON and XML."""
    if compressible and len(data) > COMPRESS_OVER:
        path = path.with_name(path.name + ".zst")
        data = zstd.ZstdCompressor(level=3).compress(data)
    write_if_changed(path, data)
    return path


def read_maybe_zstd(path: Path) -> bytes:
    data = path.read_bytes()
    if path.name.endswith(".zst"):
        return zstd.ZstdDecompressor().decompress(data)
    return data


def directory_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total
