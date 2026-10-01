"""Token buckets. Limits sit slightly under the published caps (5%)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Window:
    name: str
    limit: float
    seconds: float


@dataclass
class Bucket:
    """Sliding-window counter. ``try_consume`` is all-or-nothing across windows."""

    name: str
    windows: tuple[Window, ...]
    events: list[tuple[float, float]] = field(default_factory=list)

    def prune(self, now: float) -> None:
        horizon = max(window.seconds for window in self.windows)
        cutoff = now - horizon
        self.events = [(t, w) for t, w in self.events if t >= cutoff]

    def used(self, now: float, window: Window) -> float:
        start = now - window.seconds
        return sum(weight for t, weight in self.events if t > start)

    def try_consume(self, weight: float, now: float) -> bool:
        if weight <= 0:
            return True
        self.prune(now)
        for window in self.windows:
            if self.used(now, window) + weight > window.limit + 1e-9:
                return False
        self.events.append((now, weight))
        return True

    def refund(self, weight: float) -> None:
        if weight <= 0:
            return
        for index in range(len(self.events) - 1, -1, -1):
            if abs(self.events[index][1] - weight) < 1e-9:
                del self.events[index]
                return

    def retry_after(self, weight: float, now: float) -> float:
        """Seconds until the oldest event in a blocking window drops out."""
        self.prune(now)
        waits = [0.0]
        for window in self.windows:
            if self.used(now, window) + weight <= window.limit + 1e-9:
                continue
            in_window = sorted(t for t, _w in self.events if t > now - window.seconds)
            if in_window:
                waits.append(in_window[0] + window.seconds - now)
            else:
                waits.append(window.seconds)
        return max(waits)


def open_meteo_bucket() -> Bucket:
    """Published free caps are 600/min, 5,000/hour, 10,000/day, 300,000/month."""
    return Bucket(
        "open-meteo",
        (
            Window("minute", 570, 60),
            Window("hour", 4750, 3600),
            Window("day", 9500, 86400),
            Window("month", 285_000, 30 * 86400),
        ),
    )


def aviation_bucket() -> Bucket:
    """Published cap is 100 requests per minute."""
    return Bucket("aviationweather", (Window("minute", 95, 60),))


def ecmwf_bucket() -> Bucket:
    """No request rate is published (500 concurrent connections). The fetcher already pauses."""
    return Bucket("ecmwf-open-data", (Window("minute", 240, 60),))


def bom_bucket() -> Bucket:
    """The FTP guide publishes no cap. One connection, a modest command rate."""
    return Bucket("bom-ftp", (Window("minute", 30, 60),))


def ourairports_bucket() -> Bucket:
    return Bucket("ourairports", (Window("minute", 10, 60),))


def standard_buckets() -> dict[str, Bucket]:
    return {
        bucket.name: bucket
        for bucket in (
            open_meteo_bucket(),
            aviation_bucket(),
            ecmwf_bucket(),
            bom_bucket(),
            ourairports_bucket(),
        )
    }


def dump_buckets(buckets: dict[str, Bucket]) -> dict:
    return {
        name: [[t, w] for t, w in bucket.events]
        for name, bucket in buckets.items()
    }


def load_buckets(payload: dict | None) -> dict[str, Bucket]:
    buckets = standard_buckets()
    for name, events in (payload or {}).items():
        if name in buckets:
            buckets[name].events = [(float(t), float(w)) for t, w in events]
    return buckets
