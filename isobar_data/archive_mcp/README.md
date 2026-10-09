# Ask Isobar archive tools

Run from the isobar-data checkout:

```sh
uv run python -m isobar_data.archive_mcp --store /absolute/path/to/isobar
```

The store must already exist. No network calls, downloads, retention jobs or
archive writes are made. The server uses newline-delimited JSON-RPC on stdio:
`initialize`, `tools/list`, `tools/call`, and `ping`. Notifications receive no
reply; stdout contains protocol responses only. The MCP SDK is not a dependency,
so this package implements that small protocol directly. Supported protocol
versions: `2025-06-18`, `2025-03-26`, and `2024-11-05`.

| Tool | Purpose |
| --- | --- |
| `list_runs()` | Both grid families, retained run IDs, declared hours, actual file hours, variables/units and valid UTC times. Includes provenance retained in `thin/`. |
| `sample(var, lat, lon, valid_time_utc, run?)` | Bilinear space and linear time sampling. Stored variables include `mslp` (hPa), `t2m`/`t850` (degC), `u10`/`v10` (m/s), `tp` (mm), `gust10`, `cloud_cover` and `mucape`. `wind10` returns interpolated u/v, speed in kt, and direction **from**, degrees true. Raw u/v samples also include this derived wind. |
| `profile(lat, lon, valid_time_utc, run?)` | Pressure-level grids (currently `t850`), with all retained grid levels listed separately. Existing upper-air point JSON is returned only at its exact stored coordinate and time; other sites/levels are enumerated. |
| `section(points, valid_time_utc, var, samples=100, png=false)` | Evenly spaced samples along shortest great-circle legs, with distance in km. One family/run for the entire route. 2–2000 samples, 2–1000 waypoints; antipodal legs need another waypoint. |
| `run_change(var, lat, lon, valid_time_utc)` | Same valid time across every retained run in both families, including explicit missing/out-of-horizon values. |
| `forecast_vs_observed(station?, icao?, hours_back=48)` | WMO ID/exact name or ICAO, with surface forecast minus observation errors. UTC window ends at the current time; 1–720 hours, at most 5000 rows with an explicit truncation flag. |
| `facts(icao)` | Published OurAirports runway rows, units and publication generation. The separate static aerodrome facts catalogue is not present in this store; `aerodrome` is null with a reason. Missing runway rows are also null. |

`sample`, `profile`, `section`, and `run_change` accept optional `family`:
`ecmwf_ifs025` or `ecmwf_ifs_global`. Coordinates are degrees, longitude in
[-180,180]. UTC timestamps must have `Z` or `+00:00`. A run is `20261008T00Z`
or `ecmwf_ifs025/20261008T00Z`. Default sampling prefers the regional grid
inside its domain, otherwise global, using the newest covering run issued at
or before the valid time. No older run is silently substituted for a missing
variable/hour in the selected run. Use `run_change` to inspect alternatives.

Values carry units, run ID, run time, requested valid time and interpolation
endpoints. Null always means unavailable, with a reason; legitimate zeros remain
zero. Interpolation uses only two adjacent **listed** hours, including 144→150
when those are adjacent in the manifest. A missing listed file or weighted grid
cell makes the value null; there is no extrapolation, gap skipping or partial
stencil renormalization. Calm wind direction is null. `tp` is accumulated since
run initialization, not an hourly rain rate. Thinned data without a surviving
manifest can be sampled only at exact retained hours.

The observation reader uses the existing `products/obs/obs.sqlite` schema and
compact UTC timestamps. ICAO resolves through the published upper-air point to
an observed station within 25 km. A mapped Bureau id for an aerodrome this
store covers wins (YPPH is 94151, YSSY is 94767). Otherwise a near tie prefers
the station whose name matches a place name on the published runway or
aerodrome facts, or whose name contains "Airport" or "Aero". A unique nearest
station is labelled as that approximation. The result still reports distances,
and it says the match is ambiguous only when neither the mapping nor a name
decides the tie. WMO is preferable when station identity matters. Wind errors use components and the shortest signed
angular difference. `rain_trace` accumulates since local 09:00, so rain is shown
but its error is null because the windows differ from model `tp`.

SQLite `mode=ro` alone can create shared-memory files beside a WAL database.
The reader therefore copies a stable DB/WAL pair to an automatically deleted
temporary directory and queries that copy in read-only mode. It retries a changed
source three times, then reports unavailable. Committed live WAL rows are
included; the source DB, WAL and shared-memory files are untouched.

Matplotlib is not a project dependency. `png=true` returns `png_path: null`
and a reason, while still returning the section's JSON. No PNGs are generated.

## Daemon registration

The daemon can write a config file outside the archive and pass it to its
read-only Claude Code invocation using `--mcp-config /path/to/archive-mcp.json`:

```json
{
  "mcpServers": {
    "isobar-archive": {
      "type": "stdio",
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "run", "--directory", "/path/to/isobar-data",
        "python", "-m", "isobar_data.archive_mcp",
        "--store", "~/Data/isobar"
      ]
    }
  }
}
```

Use absolute paths for launchd, which has a minimal PATH. Point `--directory`
to the deployed checkout. Normal Claude Code tool names are
`mcp__isobar-archive__list_runs`, `mcp__isobar-archive__sample`, etc. Registering
this server does not implement the Neon queue, claim leases or reply daemon.

## Verification

```sh
uv run pytest -q tests/test_archive_mcp.py tests/test_archive_mcp_products.py tests/test_archive_mcp_protocol.py
```

Fixtures under `tests/fixtures/archive_mcp/` contain four 2×2 real-format grid
subsets (32 binary bytes). Additional controlled values use the collector's
sidecar, float16 packer and SQLite schema. Tests never read the owner's archive.
They check hand-computed interpolation, missing hours/cells, seam wrapping,
route geometry, upper-air availability, observation errors/live WAL isolation,
facts, and an actual spawned `python -m` protocol round-trip. There is no visual
interface, so no screenshots are required.
