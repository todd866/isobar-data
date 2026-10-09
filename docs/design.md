# Data design

`isobar-data` is a short-lived Python job that refreshes a local weather
archive for Isobar. The Mac app can start its bundled collector, or a separate
launchd job can run it. It takes a file lock, fetches what is
due, publishes complete products, and exits.

## Current pipeline

| Product family | Source and current scope |
| --- | --- |
| Synoptic grid | ECMWF Open Data IFS GRIB, cropped to 95°E–170°E and 0°–50°S, 0.25° grid, 00Z and 12Z runs, 0–168 h. Steps are 3 h through 144 h and 6 h from 150 h to 168 h. |
| Point forecasts | Open-Meteo ECMWF 9 km surface for 168 hours and seven local days, plus upper-air, marine, and two-point ensemble requests. Configured points are in `config/isobar.toml`. Hourly surface times stay GMT; daily dates follow each place's timezone. |
| Observations | Bureau anonymous FTP observation bundles, normalised into the local observation store. |
| Charts and warnings | Bureau anonymous FTP prognosis charts and warning XML. |
| Aviation | aviationweather.gov METAR, TAF, Australian FIR SIGMET and PIREP endpoints, plus filtered OurAirports runway data. |
| Derived coastal data | Kite spot, marine, daylight, tide and sea-breeze products derived from the point feeds. |

The scheduler keeps source state and token buckets locally. It honours source
cadences, conditional responses, retry delays and circuit breakers. Current
cadences include five-minute METAR/SIGMET checks, fifteen-minute TAF checks,
and slower model, Bureau and runway refreshes. A missed launchd interval is
handled by the next run; the job does not stay resident to catch up.

## On-disk contract

The default root is `~/Data/isobar`:

```text
status.json       health and source ages for the app
manifest.json     published product index
attribution.json  source credits and redistribution flags
state.json        scheduler watermarks, backoff and token buckets
raw/              immutable fetched bytes, retained briefly
products/         normalised products and immutable run directories
```

Writes go to temporary paths and are renamed into place only after validation.
Readers can therefore ignore an absent or incomplete product. Each product
uses its family's source, run/valid time, units and attribution fields; these
are not yet standardised across all families.
Per-family `current.json` files point at the latest complete run while keeping
older runs available for animation and comparison. New pointers and the
top-level `manifest.json` carry additive `schema_version: 1` and
`contract: "isobar-data"` markers, except the ECMWF grid, which is schema 2
so the app can see that the hour list is no longer a 3 h series. Readers
continue to accept older archives that have no marker; the marker identifies
the shared file contract without changing product payloads.

The status file is deliberately small: source id, health, run, availability,
age label and detail. A stale source retains its last good run and reports the
reason; it is not silently presented as current.

## App follow-up: 168 h grid

The chart's frame list is no longer "33 steps times 3 h". Grid `current.json`
and each run's `manifest.json` are schema 2 (`schema_version` 2, `contract`
`isobar-data`, `family` `grids/ecmwf_ifs025`). Schema 1 is the old 0–96 h grid.
An app that only accepts schema 1 will refuse the new pointer. That closed
failure is intentional until the reader is updated.

`forecast_hours` is the ladder: `0, 3, …, 144, 150, 156, 162, 168`.
`uniform_step_hours` is null. Each field sidecar carries `lead_hours` for that
file and does not claim a 3 h step.

The app should:

- Read the ladder from the run manifest. Do not build times as `index * 3 h`,
  and do not keep `kFrameHorizon` at 96 h. The horizon is 168 h. The frame
  count is `len(forecast_hours)` (53 on this ladder).
- Interpolate only between adjacent listed hours, using the gap those two
  hours actually have: 3 h from 0 through 144, then 6 h through 168.
- If a listed hour has no file, stop. Do not write a zero grid and do not
  stretch the previous hour across the gap.
- Build the 24 h rain total by finding the frame 24 h earlier in
  `forecast_hours`. Eight frames back is 24 h only while the step stays 3 h.
  It is wrong on the 6 h tail: 150 h pairs with 126 h, not with the frame
  eight indexes earlier.
- Ignore 06Z and 18Z for this product. They end at 144 h, so the collector
  publishes the newest 00Z and 12Z only.
- Open the run named `latest` and read that directory's `manifest.json`. A
  run directory with no manifest is the old 0–96 h grid; do not invent the
  later hours for it. The previous run is only the source of the first day's
  24 h rain.

## Storage and retention

Raw provider responses are kept only long enough to reprocess a bad
normalisation. Model and aviation raw data normally retain 14 days; Bureau
tarballs retain three days; ECMWF GRIB retains 48 hours. Normalised grid and
point products retain the runs needed by the app's forecast window and recent
animation. Partial downloads expire after one hour. Retention runs only after
a successful publication and refuses to cross the configured store or free
space limits.

The scheduler uses a lock, idempotent object keys, temporary files and
provider-aware backoff. A provider block stops that provider rather than
rotating user agents or retrying aggressively. The configured request budgets
are documented in [budget.md](budget.md); source terms and credits are in
[sources.md](sources.md).

## Aviation and coastal boundaries

METAR parsing keeps raw visibility, wind, gust, cloud layers and ceiling
semantics. TAF layers remain separate from model cloud fractions. SIGMETs are
filtered to the Australian FIRs used by Isobar. Runway crosswind is derived
from true headings and the observed wind; the product does not choose a
runway. PIREP emptiness is a normal result.

The archive is situational awareness data. It is not a flight briefing and
does not provide NOTAM/NAIPS authority, alternate minima, runway selection or
an official tide table. Bureau material remains subject to its terms and is
not a public redistribution feed. NOTAC is an optional, user-keyed adapter;
it is not enabled by the scheduler and is not an authority replacement.

## Global ECMWF family

The collector publishes `grids/ecmwf_ifs_global` alongside the Australian
`grids/ecmwf_ifs025` family. Both use schema 2 and the same 53 listed leads.
The global target is a seam-free 0.5° regular grid: west −180, east 179.5,
north 90, south −90, 720 × 361, with `wraps_longitude: true`. It is produced
by serially downsampling the cached real 0.25° ECMWF GRIB field; each target
point selects its nearest source grid node, with no cell averaging or
interpolation. The source and target steps are recorded in the manifest and
sidecars. The two families share
raw index/message downloads and publish independently only after all required
fields are complete. Readers must select a family explicitly and validate its
coverage rather than treating the Australian crop as global.

The global pointer retains the five newest complete runs, ordered by run time
even if an older collection finishes late. Earlier runs supply the first day's
24-hour rain windows. Publication does not delete files; unreferenced old runs
remain subject to the existing archive size budget. The regional family keeps
its existing retention policy.

The Mac app and web site should read only products that pass their family
validation and carry their source credit.

See [plan.md](plan.md) for the ordered implementation work. The document in
`docs/archive/design-2026-09-26.md` is the historical design discussion; it
contains the earlier 1° Open-Meteo grid proposal and is not current behavior.
