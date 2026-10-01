"""Bureau anonymous FTP. Never the website and never the JSON API."""

from __future__ import annotations

import io
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import ftplib

from isobar_data.normalise import (
    connect_obs,
    insert_obs,
    is_warning_name,
    rows_from_obs_document,
    warning_identifier,
    warning_title,
)
from isobar_data.policy import FTP_HOST, assert_ftp_allowed
from isobar_data.publish import commit_files, read_pointer
from isobar_data.storage import write_if_changed

import json


class BureauBlock(RuntimeError):
    """The Bureau closed the connection. Stop. Do not rotate identities."""


class FtpSession:
    def __init__(self, ftp):
        self.ftp = ftp

    def size(self, name: str) -> int:
        return int(self.ftp.size(name))

    def mdtm(self, name: str) -> str:
        reply = self.ftp.sendcmd(f"MDTM {name}")
        # 213 YYYYMMDDHHMMSS
        return reply.split()[-1]

    def retr(self, name: str) -> bytes:
        buffer = io.BytesIO()
        self.ftp.retrbinary(f"RETR {name}", buffer.write)
        return buffer.getvalue()

    def nlst(self) -> list[str]:
        names: list[str] = []
        self.ftp.retrlines("NLST", names.append)
        return names

    def close(self) -> None:
        try:
            self.ftp.quit()
        except Exception:
            self.ftp.close()


def connect_ftp(timeout: float = 60) -> FtpSession:
    assert_ftp_allowed(FTP_HOST)
    ftp = ftplib.FTP(FTP_HOST, timeout=timeout)
    ftp.login("anonymous", "isobar-data@personal")
    ftp.set_pasv(True)
    ftp.cwd("/anon/gen/fwo")
    return FtpSession(ftp)


def _guard(exc: Exception) -> None:
    text = str(exc).lower()
    if isinstance(exc, ftplib.error_temp) and (str(exc).startswith("421") or "blocked" in text or "ban" in text):
        raise BureauBlock(str(exc)) from exc
    if "blocked" in text or "denied" in text and "421" in text:
        raise BureauBlock(str(exc)) from exc


def fetch_if_changed(session: FtpSession, name: str, previous: dict | None) -> tuple[str, bytes | None, dict]:
    """Return ``unchanged``, ``downloaded``, or ``missing``.

    ``previous`` is ``{"mdtm", "size"}`` from the last stored object.
    """
    try:
        size = session.size(name)
        mdtm = session.mdtm(name)
    except ftplib.error_perm as exc:
        if str(exc).startswith("550"):
            return "missing", None, previous or {}
        _guard(exc)
        raise
    except ftplib.error_temp as exc:
        _guard(exc)
        raise
    stamp = {"mdtm": mdtm, "size": size}
    if previous and previous.get("mdtm") == mdtm and int(previous.get("size") or -1) == size:
        return "unchanged", None, stamp
    try:
        body = session.retr(name)
    except ftplib.error_temp as exc:
        _guard(exc)
        raise
    return "downloaded", body, stamp


def store_tarball(root: Path, name: str, mdtm: str, body: bytes) -> Path:
    path = root / "raw" / "bom" / name / f"{mdtm}.tgz"
    write_if_changed(path, body)
    return path


def unpack_obs(body: bytes, connection, default_product: str) -> dict:
    archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    inserted = 0
    coords = {}
    for member in archive.getmembers():
        if not member.isfile() or not member.name.endswith(".json"):
            continue
        extracted = archive.extractfile(member)
        if extracted is None:
            continue
        try:
            document = json.loads(extracted.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            continue
        rows, station = rows_from_obs_document(document, default_product)
        inserted += insert_obs(connection, rows)
        if station and station.get("lat") is not None:
            coords[int(station["wmo"])] = {"lat": station["lat"], "lon": station["lon"], "name": station.get("name")}
    return {"inserted": inserted, "coords": coords}


def store_chart(root: Path, name: str, body: bytes, mdtm: str) -> None:
    raw = root / "raw" / "charts" / name / f"{mdtm}{Path(name).suffix}"
    write_if_changed(raw, body)
    _adopt_legacy_charts(root)
    files = _published_bytes(root, "charts", {".pdf", ".gif"})
    if files.get(name) == body:
        return
    files[name] = body
    commit_files(root, "charts", files, keep="previous")


def store_warning(root: Path, name: str, body: bytes, mdtm: str) -> dict:
    raw = root / "raw" / "warnings" / name / f"{mdtm}.xml"
    text = body.decode("utf-8", "replace")
    from isobar_data.storage import store_raw

    store_raw(raw, body, compressible=True)
    files = _published_bytes(root, "warnings", {".xml"})
    if files.get(name) != body:
        files[name] = body
        commit_files(root, "warnings", files, keep="latest")
    return {"id": warning_identifier(text) or Path(name).stem, "title": warning_title(text)}


def _published_bytes(root: Path, family: str, suffixes: set[str]) -> dict[str, bytes]:
    from isobar_data.publish import run_dir

    latest = read_pointer(root, family).get("latest")
    if latest:
        directory = run_dir(root, family, latest)
    else:
        directory = root / "products" / family
    return _direct_files(directory, suffixes)


def _direct_files(directory: Path, suffixes: set[str]) -> dict[str, bytes]:
    if not directory.is_dir():
        return {}
    found = {}
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in suffixes:
            found[path.name] = path.read_bytes()
    return found


def _adopt_legacy_charts(root: Path) -> None:
    """Copy an on-disk current/previous pair into runs without editing those files."""
    if read_pointer(root, "charts")["runs"]:
        return
    current = _direct_files(root / "products" / "charts", {".pdf", ".gif"})
    previous = _direct_files(root / "products" / "charts" / "previous", {".pdf", ".gif"})
    if previous:
        commit_files(root, "charts", previous, keep="latest")
    if current:
        commit_files(root, "charts", current, keep="previous" if previous else "latest")


def sync_obs(root: Path, session: FtpSession, names: tuple[str, ...], state: dict) -> dict:
    ftp_state = state.setdefault("ftp", {})
    connection = connect_obs(root / "products" / "obs" / "obs.sqlite")
    downloaded = 0
    skipped = 0
    missing = []
    try:
        for name in names:
            status, body, stamp = fetch_if_changed(session, name, ftp_state.get(name))
            if status == "unchanged":
                ftp_state[name] = stamp
                skipped += 1
                continue
            if status == "missing" or body is None:
                missing.append(name)
                continue
            store_tarball(root, name, stamp["mdtm"], body)
            product = Path(name).stem
            summary = unpack_obs(body, connection, product)
            coords = state.setdefault("coords", {})
            for wmo, row in summary["coords"].items():
                coords[str(wmo)] = row
            ftp_state[name] = stamp
            downloaded += 1
    finally:
        connection.close()
    latest = _latest_obs_time(root)
    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "missing": missing,
        "run": latest,
    }


def sync_charts(root: Path, session: FtpSession, names: tuple[str, ...], state: dict) -> dict:
    ftp_state = state.setdefault("ftp", {})
    downloaded = 0
    skipped = 0
    for name in names:
        status, body, stamp = fetch_if_changed(session, name, ftp_state.get(name))
        if status == "unchanged":
            ftp_state[name] = stamp
            skipped += 1
            continue
        if body is None:
            continue
        store_chart(root, name, body, stamp["mdtm"])
        ftp_state[name] = stamp
        downloaded += 1
    return {"downloaded": downloaded, "skipped": skipped, "run": _newest_mdtm(names, ftp_state)}


def sync_warnings(root: Path, session: FtpSession, state: dict) -> dict:
    names = [name for name in session.nlst() if is_warning_name(name)]
    ftp_state = state.setdefault("ftp", {})
    downloaded = 0
    skipped = 0
    titles = []
    for name in sorted(names):
        status, body, stamp = fetch_if_changed(session, name, ftp_state.get(name))
        if status == "unchanged":
            ftp_state[name] = stamp
            skipped += 1
            continue
        if body is None:
            continue
        titles.append(store_warning(root, name, body, stamp["mdtm"]))
        ftp_state[name] = stamp
        downloaded += 1
    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "count": len(names),
        "titles": titles,
        "run": _newest_mdtm(names, ftp_state),
    }


def _latest_obs_time(root: Path) -> str | None:
    path = root / "products" / "obs" / "obs.sqlite"
    if not path.is_file():
        return None
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT MAX(aifstime_utc) FROM obs").fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        return None
    moment = datetime.strptime(row[0], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _newest_mdtm(names, ftp_state) -> str | None:
    stamps = []
    for name in names:
        mdtm = (ftp_state.get(name) or {}).get("mdtm")
        if mdtm:
            stamps.append(mdtm)
    if not stamps:
        return None
    latest = max(stamps)
    moment = datetime.strptime(latest, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
