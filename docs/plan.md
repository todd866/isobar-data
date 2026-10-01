# Data plan

This is the working order for the collector. It describes the current
Australian product set and the contracts needed before expanding it.

## Now: make the existing archive dependable

1. Keep the ECMWF regional GRIB crop as the chart's default source: 00Z and
   12Z runs, 0–96 hours, three-hour native steps.
2. Keep Open-Meteo point products within the configured caps: surface spots,
   aerodromes, upper air, marine and ensemble at Swanbourne and Sydney.
3. Keep Bureau observations, charts and warnings on the anonymous FTP paths
   documented in `sources.md`.
4. Keep aviationweather.gov METAR, TAF, SIGMET and PIREP products separate
   from Bureau observations, with raw text retained for auditability.
5. Add product-family schemas and fixtures for `current.json`, grids,
   aerodromes, aviation notices, observations, kite and marine products.
6. Make the app's source-age, missing-product and attribution states easy to
   test against a temporary archive.

The collector should remain safe to run repeatedly: unchanged responses are
not rewritten, incomplete runs do not become current, and backoff survives a
restart. The test suite uses fixtures and temporary stores; it does not need
provider accounts.

## Next: broaden the data contract

- Generalise the ECMWF crop and grid metadata so a configured region can be
  added without hardcoding Australia into the reader.
- Version each product family independently and validate the family before
  updating its `current.json`. New pointers and the top-level manifest now
  carry additive `schema_version` and `contract` markers; payload validation
  remains a follow-up for each family.
- Add international place metadata, local timezone labels and clear source
  coverage states before presenting a place outside the configured region.
- Add a bounded history policy for animated maps and a fixture that exercises
  interpolated display frames without claiming they are model output.

## Later, only with evidence

RainViewer, a second model cross-check, extra ECMWF cycles or satellite
imagery may be useful after the current archive shows a real need. Each one
needs a live source check, a documented licence, a request budget and a
product fixture before it becomes a scheduled source. Native ECMWF GRIB is
already used for the regional chart; expanding its region or variables is a
separate storage and processing decision.

## Out of scope

This project does not scrape `bom.gov.au`, call `api.weather.bom.gov.au`,
pull NAIPS, or present NOTAM data as an official briefing. It does not publish
Bureau personal-use files as a public data service. A public API, always-on
host and global weather coverage can be considered later once the local
product contracts and source terms support them.

The previous design and plan are preserved in
[`docs/archive/`](archive/plan-2026-09-26.md) and are labelled historical.
