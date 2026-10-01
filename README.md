# isobar-data

Sustainable ingestion of weather data for [Isobar](https://github.com/todd866/isobar), the macOS menu-bar synoptic chart. The daemon is `isobar-data`: one short-lived process, `uv run isobar-data`, writing `~/Data/isobar`.

| Document | What it decides |
| --- | --- |
| [docs/sources.md](docs/sources.md) | Each feed, checked with a small live request on 26 September 2026, with the licence and the limit quoted from the publisher. |
| [docs/design.md](docs/design.md) | Scheduler, `~/Data/isobar` layout, and the file contract Isobar can read. Python 3.12+ with uv. |
| [docs/budget.md](docs/budget.md) | Current point-request update followed by historical budget arithmetic. |
| [docs/plan.md](docs/plan.md) | Current work and the next data-contract/international milestones. |

The collector stores the national chart from ECMWF Open Data: IFS 0.25°, cropped to 95°E–170°E and 0°–50°S, the latest 00Z and 12Z runs, steps 0–96 h at 3 h. Open-Meteo stays on the budgeted point products (9 km spots and aerodromes, upper air, marine, ensemble at two pins). Bureau observations, warnings and prognosis come from anonymous FTP only. METAR, TAF and Australian-FIR SIGMETs come from aviationweather.gov. The earlier 1° Open-Meteo grid design is archived; that grid is not fetched.

Run `uv sync --frozen --group dev` and `uv run --frozen pytest -q` for the full
collector test suite. GitHub runs it on pushes and pull requests; tests use
fixtures and temporary archives, not live provider accounts.

`uv sync`, then `uv run isobar-data`. `scripts/install-launchd.sh` copies the package into `~/Library/Application Support/isobar-data`, creates a uv virtualenv there, writes a LaunchAgent that points at that copy, and does not load it. `scripts/uninstall-launchd.sh` removes both. `fetch-ecmwf` remains the one-shot float32 crop used by the chart prototype. Bureau radar, Bureau tide pages, NAIPS, `www.bom.gov.au`, and `api.weather.bom.gov.au` are out.

Hosted CI runners with less than the laptop default free-space reserve can set
`ISOBAR_FREE_SPACE_FLOOR_GB` to a finite value of at least `2` before running
the collector. The same threshold guards scheduler fetches and the ECMWF
large-fetch check; when unset it remains `20` GB. Invalid values fail before a
source request starts.


## Aviation notices

METAR and Australian-FIR SIGMET checks run every five minutes; TAF runs every
fifteen minutes. The LaunchAgent wakes every five minutes and the scheduler
keeps each other source on its own cadence.

`uv run isobar-data import-notams --file briefing.txt --source "Briefing export"`
imports ICAO plaintext or canonical JSON into the versioned aviation snapshot.
Each import replaces the previous NOTAM briefing, retaining unrelated aviation
products. Raw text, issue identifiers, locations, dates, schedules, vertical
limits and Q-line coordinates are retained. Replacement/cancellation resolution
requires an unambiguous matching identifier and location. Unknown times stay
unknown and estimated ends are not treated as automatic expiry.

### NOTAC connection

The NOTAC adapter uses its [published API](https://notac.aero/api/). Create a
free account at [notac.aero](https://notac.aero/) and make a key from the
Account menu. Each pilot uses their own key. NOTAC confirmed Australian
coverage, including YPPH, YPJT and YMMM, on 28 September 2026. We have not
tested a live key yet. The current connection is a collector command; the Mac
app does not manage API keys.

Run a coverage check first:

```sh
uv run isobar-data fetch-notams --dry-run
```

Enter the key at the hidden prompt. It stays in memory; it is never written to
configuration, the archive or logs. A helper can instead send the key through
stdin with `--token-stdin`. Do not put keys in command arguments, shell history
or repository files.

The default query covers notices filed against Perth (`YPPH`), Jandakot (`YPJT`)
and the Melbourne FIR (`YMMM`), overlapping the next 72 hours. `YMMM` here means
FIR-wide notices, **not every airport inside the FIR**. Use `--locations` and
`--hours` to change the query. The report counts records by location, requests
and estimated credits. A successful query or zero results does not prove that
the provider holds every notice issued by the authority.

After inspecting coverage, omit `--dry-run` to publish into Isobar's existing
NOTAM viewer. Each successful fetch replaces the previous NOTAM snapshot,
including with an empty result. Failed or incomplete downloads preserve it;
METAR, TAF and SIGMET products remain intact. The product records the queried
locations and time window. Original notice text is retained and NOTAC's generated
readings are not used.

The initial adapter uses a full bounded search, including upcoming notices, with
detail lookups when the search omits original text. It stops at 150 requests
(`--max-requests` can set 1–200), never follows redirects, and does not retry
rejected keys, exhausted credits or rate limits. NOTAC documents 10,000 free
credits/month during beta; search pages cost two, detail requests one. Even a
dry run uses credits. Scheduled polling is not enabled until live response sizes
and the account budget have been checked.
Pagination checks catch changing counts and repeated IDs. The API does not
document a transactional snapshot, so a same-count change during pagination may
still escape detection. Live acceptance must also check withdrawals and notices
whose estimated end has passed before scheduled polling is enabled.

NOTAC attribution is retained as `NOTAC · unofficial`. Its
[terms](https://notac.aero/terms/) allow storage and display with attribution and
that source status; it does not offer an official briefing feed. The adapter
does not use NAIPS or require NAIPS credentials.

## Atmospheric profiles

Upper-air products retain temperature, geopotential height, relative humidity,
cloud fraction, horizontal wind and geometric vertical velocity at 13 levels:
1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100 and 50 hPa.
Wind speed is in knots. Vertical velocity is in m/s, positive upward; it is
Open-Meteo’s conversion of ECMWF pressure velocity, not native omega in Pa/s.
Heights are metres above mean sea level; model terrain is not airport elevation.
Missing values remain missing. Surface products also retain low, middle and high
cloud fraction. These complement the airport TAF/METAR; the collector does not
yet ingest the Bureau GAF.
