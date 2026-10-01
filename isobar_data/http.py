"""Conditional GET, full-jitter backoff, and the single User-Agent."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import httpx

from isobar_data.identity import USER_AGENT
from isobar_data.policy import PolicyError, assert_http_allowed
from isobar_data.tokens import Bucket

# In-process sleeps stay short. A longer penalty is handed back to the scheduler.
IN_PROCESS_SLEEP = 45
BACKOFF_CAP = 30 * 60
RETRY_STATUSES = {429, 500, 502, 503, 504}


class Later(Exception):
    """Try this source again at ``when``.

    ``failure`` is set when a request was sent and the server asked us to wait.
    A token-bucket deferral is not a failure.
    """

    def __init__(self, when: datetime, reason: str, *, failure: bool = False):
        super().__init__(reason)
        self.when = when
        self.reason = reason
        self.failure = failure


class Blocked(RuntimeError):
    """The server refused the client. Do not rotate the User-Agent."""


@dataclass
class Stats:
    http: int = 0
    open_meteo_calls: float = 0.0
    bytes: int = 0
    ftp_downloads: int = 0
    ftp_skipped: int = 0
    by_host: dict[str, int] = field(default_factory=dict)


class Http:
    def __init__(
        self,
        buckets: dict[str, Bucket],
        stats: Stats,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        uniform=random.uniform,
        now=None,
        timeout: float = 60,
    ):
        self.buckets = buckets
        self.stats = stats
        self.sleep = sleep
        self.uniform = uniform
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.exchange("GET", url, **kwargs)

    def head(self, url: str, **kwargs) -> httpx.Response:
        return self.exchange("HEAD", url, **kwargs)

    def exchange(
        self,
        method: str,
        url: str,
        *,
        bucket: str,
        weight: float = 1,
        etag: str | None = None,
        modified: str | None = None,
        timeout: float | None = None,
        accept: str | None = None,
    ) -> httpx.Response:
        assert_http_allowed(url)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        if accept:
            headers["Accept"] = accept
        delay_base = 1.0
        last = "no response"
        try:
            for _attempt in range(5):
                try:
                    response = self._send(method, url, headers, timeout, bucket, weight)
                except Later:
                    raise
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last = str(exc)
                    self._wait_or_defer(0, delay_base, bucket, weight, failure=True)
                    delay_base = min(delay_base * 2, BACKOFF_CAP)
                    continue
                if response.status_code in RETRY_STATUSES:
                    last = f"HTTP {response.status_code}"
                    retry_after = _retry_after_seconds(response, self.now())
                    self._wait_or_defer(0, delay_base, bucket, weight, retry_after, failure=True)
                    delay_base = min(delay_base * 2, BACKOFF_CAP)
                    continue
                if response.status_code in {401, 403}:
                    raise Blocked(f"HTTP {response.status_code} from {url}; not retrying with another agent")
                return response
        except Later:
            raise
        raise RuntimeError(f"{url} failed after retries ({last})")

    def _send(
        self,
        method: str,
        url: str,
        headers: dict,
        timeout: float | None,
        bucket: str,
        weight: float,
    ) -> httpx.Response:
        current = url
        response = None
        for _hop in range(4):
            if weight > 0 and not self._consume(bucket, weight):
                raise Later(self._defer_until(bucket, weight), f"{bucket} bucket is empty")
            host = httpx.URL(current).host or ""
            self.stats.http += 1
            self.stats.by_host[host] = self.stats.by_host.get(host, 0) + 1
            response = self._client.request(method, current, headers=headers, timeout=timeout)
            self.stats.bytes += len(response.content or b"")
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            current = urljoin(current, location)
            assert_http_allowed(current)
            method = "GET"
        raise RuntimeError(f"too many redirects from {url}")

    def _consume(self, bucket: str, weight: float) -> bool:
        if weight <= 0:
            return True
        ok = self.buckets[bucket].try_consume(weight, self.now().timestamp())
        if ok and bucket == "open-meteo":
            self.stats.open_meteo_calls += weight
        return ok

    def _defer_until(self, bucket: str, weight: float) -> datetime:
        seconds = self.buckets[bucket].retry_after(weight, self.now().timestamp())
        return self.now() + timedelta(seconds=max(seconds, 60))

    def _wait_or_defer(
        self,
        attempt: int,
        base: float,
        bucket: str,
        weight: float = 1,
        retry_after: float | None = None,
        *,
        failure: bool = False,
    ) -> None:
        del attempt, weight
        if retry_after is not None:
            delay = max(0.0, float(retry_after))
        else:
            delay = self.uniform(0, min(BACKOFF_CAP, base))
        if delay > IN_PROCESS_SLEEP:
            raise Later(
                self.now() + timedelta(seconds=delay),
                f"backing off {bucket} for {int(delay)}s",
                failure=failure,
            )
        if delay > 0:
            self.sleep(delay)


def _retry_after_seconds(response: httpx.Response, now: datetime) -> float | None:
    header = response.headers.get("retry-after")
    if header is None or not header.strip():
        return None
    text = header.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds())
