"""Hosts the daemon is allowed to touch.

Bureau observations, warnings and prognosis come from anonymous FTP only.
The website and the public JSON API are out of bounds.
"""

from __future__ import annotations

from urllib.parse import urlparse

FTP_HOST = "ftp.bom.gov.au"


class PolicyError(RuntimeError):
    """A URL the archive must not request."""


def assert_http_allowed(url: str) -> None:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host == "bom.gov.au" or host.endswith(".bom.gov.au"):
        raise PolicyError(
            f"refusing {host}: Bureau bytes come from anonymous FTP at {FTP_HOST} only"
        )


def assert_ftp_allowed(host: str) -> None:
    if host.lower().rstrip(".") != FTP_HOST:
        raise PolicyError(f"refusing FTP host {host}: only {FTP_HOST} is allowed")
