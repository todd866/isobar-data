# Data design

`isobar-data` is a short-lived Python job that refreshes a local weather
archive for Isobar. The Mac app can start its bundled collector, or a separate
launchd job can run it. It takes a file lock, fetches what is
due, publishes complete products, and exits.

## Current pipeline

| Product family | Source and current scope |
| --- | --- |
| Synoptic grid | ECMWF Open Data IFS GRIB, cropped to 95°E–170°E and 0°–50°S, 0.25° grid, 00Z and 12Z runs, 0–96 hours at the model's three-hour steps. |
| Point forecasts | Open-Meteo ECMWF 9 km surface, upper-air, marine, and two-point ensemble requests. Configured points are in `config/isobar.toml`. |
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
`contract: "isobar-data"` markers. Readers continue to accept older archives
that have no marker; the marker identifies the shared file contract without
changing product payloads.

The status file is deliberately small: source id, health, run, availability,
age label and detail. A stale source retains its last good run and reports the
reason; it is not silently presented as current.

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

## Next architecture work

The next data work is to make product-specific schemas explicit, add fixtures
for every published family, and generalise the regional ECMWF crop pipeline.
International place selection can then choose a configured region without
pretending the current Australian crop is global coverage. The Mac app and web
site should read only products that pass their family validation and carry
their source credit.

See [plan.md](plan.md) for the ordered implementation work. The document in
`docs/archive/design-2026-09-26.md` is the historical design discussion; it
contains the earlier 1° Open-Meteo grid proposal and is not current behavior.
