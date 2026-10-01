## Atmosphere inspector update — 27 September 2026

The current collector uses ECMWF Open Data for the national grid; the historical
Open-Meteo grid arithmetic below is retained as the earlier design record.
Upper-air point requests now contain 91 hourly variables (13 pressure levels,
including relative humidity, cloud fraction, wind and vertical velocity), weight
9.1 per location/run.
At the eight-airport cap and four runs this is 291.2 weighted calls/day; the
configured two airports use 72.8. Surface points request 168 hourly hours,
seven local calendar days, and ten daily fields (14 hourly + 10 daily = 24
names). Seven days stays inside the 14-day weight plateau, so the weight is
2.4 per location. At the eight-point cap and four runs that is 76.8/day; the
configured six points are 57.6/day. Upper air, marine, ensemble, those surface
runs, and the 15-minute meta checks stay under the token-bucket day and month
windows. The 72-hour surface horizon in the historical arithmetic below is not
the current request.
The collector calculates request weight from the actual variable count.
Changing the requested variable set permits one refresh of the current run;
subsequent calls reuse that run until the publisher advances it.

# Budget

v0 only. Figures are from the 26 September 2026 checks in `sources.md`, or they are marked as arithmetic on those figures. Open-Meteo call weight is the pricing-page formula:

```text
calls = max(1, (variables × models / 10) × max(1, days / 14)) × locations
```

Free caps, all Open-Meteo hosts combined: under 10,000 calls/day, 5,000/hour, 600/minute, and 300,000/month on the pricing table.

## v0 sources

Phase 1b keeps the same free caps and adds the pilot and kite fields. The national grid drops from four cycles to the 00Z and 12Z cycles so the daily and monthly totals stay at or under half the cap. The afternoon sea breeze is carried by the 9 km points, which still run four times a day.

| # | Pull | Why this size |
| --- | --- | --- |
| 1 | IFS 0.25° grid, 10°S–45°S, 110°E–160°E, 1.0°, 00Z and 12Z | 36 × 51 = **1,836** locations. Ten variables, still weight 1. |
| 2 | IFS 0.25° ensemble at 2 points | Swanbourne and Sydney Observatory Hill. Not a grid. |
| 3 | IFS 9 km surface points, cap 8 | Beaches, sea-breeze pair, YSSY. 14 names, weight 1.4, four runs, 72 h. |
| 4 | IFS 0.25° upper air, cap 8 aerodromes | 850/700/500/300 plus 1000 and 925 heights. 20 names, weight 2, four runs. |
| 5 | Marine Best Match, cap 4 points | Waves, swell, water temperature, hourly sea level. 14 names, weight 1.4, four runs. |
| 6 | Bureau `IDW60910.tgz` and `IDN60910.tgz` | Half-hourly obs, including the coastal WMO ids. WA size measured; NSW not. |
| 7 | Bureau `IDG00073.pdf`, `IDG00074.gif`, warning XML | FTP, when `Last-Modified` changes. |
| 8 | METAR and TAF, cap 8 aerodromes, plus SIGMET | One HTTP request for the list. FIR filter is local. |

Out of this budget on purpose: Bureau radar, Bureau tide pages, NAIPS, AIRMET, GPWT, `api.weather.bom.gov.au`, `IDY00050`, ACCESS-G, GRIB, RainViewer, GIBS, Himawari L1b, and the 06Z and 18Z national grids.

## Calls per day

Weight stays 1 while `variables × models / 10` is at most 1 and the horizon is at most 14 days. The grid uses that plateau: 10 variables, 1 model, 48 hours, weight **1** per location. Two runs: **3,672** calls/day. Four runs would be 7,344 for the grid alone.

The 600/minute cap counts these calls, not HTTP requests. 1,836 in one minute is over the cap. Send chunks of at most 250 locations and wait a minute between chunks. The point bundle on the same minute is about 35 calls, and 250 + 35 is under half of 600. One national pull then takes about eight minutes. It is nowhere near 5,000 in one hour: 1,836 plus the point cycle is about 1,870.

Surface points: 14 names (12 hourly plus sunrise and sunset), 72 hours, weight 1.4. Cap 8 locations × 4 runs = **44.8**/day. This assumes a `daily` name counts as a variable. If it does not, the real total is lower.

Upper air: 20 names, 72 hours, weight 2. Cap 8 × 4 runs = **64**/day.

Marine: 14 names, Best Match counted as **one** model, weight 1.4. Cap 4 × 4 runs = **22.4**/day. If Best Match is billed as several models, this row grows and the remainder below still covers a doubling.

Ensemble: 2 variables, 7 days, 2 locations, weight 1. **8**/day.

| Open-Meteo | Calls/day | Calls/month (×30) |
| --- | ---: | ---: |
| 1° grid × 2 runs | 3,672 | 110,160 |
| Surface points, cap 8 × 4 | 44.8 | 1,344 |
| Upper air, cap 8 × 4 | 64 | 1,920 |
| Marine, cap 4 × 4 | 22.4 | 672 |
| Ensemble × 2 points × 4 | 8 | 240 |
| Subtotal | 3,811.2 | 114,336 |
| Retries, say 5% | 190.6 | 5,718 |
| **Total** | **4,001.8** | **120,054** |
| Cap | 10,000 | 300,000 |
| Left | 5,998.2 (59.98%) | 179,946 (59.98%) |

4,001.8 is 40% of the daily cap and of the monthly cap. The spare is 59.98% of each. That is the headroom rule for this phase. It is not room for a second national grid. GFS 0.25° at the same 1,836 points and two runs is another 3,672 calls. That fits inside the 5,998 unused calls and would leave about 23% spare, so it is not in v0. Adding the 06Z grid cycle is 1,836 × 1.05 ≈ 1,930 calls and would drop the daily spare from 59.98% to 40.7%. That is why 06Z and 18Z are outside v0.

The caps above are the configured maximum, not the two-aerodrome default. YPPH and YSSY alone, and two marine points, would use less. The budget is the cap so a third aerodrome does not silently spend the spare.

### What does not fit

| Idea | Calls per run | ×4 runs |
| --- | ---: | ---: |
| Same window at 0.5° (71 × 101 = 7,171) | 7,171 | 28,684 |
| Same window at 0.25° (141 × 201 = 28,341) | 28,341 | 113,364 |
| Ensemble on the 1° grid (still × locations, not × 51) | 1,836 | 7,344 |

0.5° once a day is 7,171. That is inside the raw cap and outside the 50% headroom rule, so it is not a v0 switch. 0.25° national does not fit the free tier at all. The paid Standard plan is €29/month and the pricing table says it does **not** include ensemble. Professional is €99/month, 5M calls, ensemble included. A 0.25° grid four times a day is about 113,000 calls/day, about 3.4 million a month, which is inside Professional’s 5M and far outside free. Downloading ECMWF Open Data is the other way to get a real 0.25° grid, and the step-0 file was 129 MB.

## Other services

| Source | Requests | Limit found |
| --- | --- | --- |
| aviationweather METAR+TAF, hourly, up to 8 airports, one request each | 48/day, plus a 304 on most of them | 100 per minute |
| SIGMET body | 2/hour, about 82 KB on the phase 1b fetch, filtered locally | same |
| PIREP YPPH | 24/day, last check was HTTP 204 | same |
| AIRMET endpoint | 0 | The feed that answered was not Australian |
| OurAirports `runways.csv` | one HEAD a week; a 4.0 MB GET when `Last-Modified` moves | no number on the data page |
| Bureau FTP | 2 tarballs × 48, plus charts and warnings when the timestamp moves | no number in the Feb 2026 guide; one connection |
| Bureau tide pages, NAIPS, GPWT, AIRMET pages | 0 | terms in `sources.md` |
| ECMWF portal, NOMADS, GIBS, RainViewer, Himawari | 0 in v0 | see `sources.md` |

## Bytes

Measured anchors:

| Object | Bytes |
| --- | ---: |
| 1 point, 24 h, 4 variables, IFS 0.25° | 1,333 |
| 16 points, 1 h, 2 variables | 5,638 |
| 1 point, 7 days, ensemble MSLP, 51 series | 66,222 |
| 1 point, 15 days, MSLP | ~9,600 |
| `IDW60910.tgz` | 2,943,378 |
| `IDG00073.pdf` / `IDG00074.gif` | 621,172 / 147,087 |
| Swanbourne capital-city JSON | 112,565 |
| METAR YPPH+YSSY / TAF both | 831 / 6,191 |
| 9 km wind, 72 h, 3 variables, 1 point | 2,672 |
| Marine, 72 h, wave + swell + SST, 1 point | 2,812 |
| `isigmet` worldwide JSON | 81,545 |
| OurAirports `runways.csv` (filtered after download) | 3,966,309 |
| IFS 0.25° GRIB step 0 (not downloaded in v0) | 134,732,912 |
| GFS 0.25° GRIB f000 (not downloaded in v0) | 484,604,024 |

**Grid JSON, estimated.** The 1,333 byte sample is one point and 24 hours. A 48-hour, four-variable point is taken as **2.5 KB**, a bit under twice that sample, because the keys are not repeated. Ten variables is taken as 2.5 × 10/4 = **6.3 KB** per point. 1,836 × 6.3 KB ≈ **11.5 MB per run**, **23 MB/day** at two runs, **0.32 GB per 14 days**. This was not measured at 1,836 points or at ten variables.

**Ensemble JSON, estimated.** 66 KB was one variable and one point. Two variables and two points is taken as **0.26 MB per run**, **1 MB/day**.

**Bureau obs.** Downloading the WA tarball every half hour is 48 × 2.94 MB = **141 MB/day**. NSW was not measured; if it matches WA, another 141 MB/day. Most of that is the same 72-hour history shipped again. Unique new observations are on the order of a third of one tarball per day (**about 1 MB per state**), once rows are deduplicated on `(wmo, time)`. The SQLite file is that, not the tarball.

**Charts.** One observed issue of the two prognosis files is 0.77 MB. If the Bureau replaces them four times a day, about **3 MB/day**. Cadence was not measured; only one `Last-Modified` (04:13Z) was seen.

**Aviation.** Under **1 MB/day** even without 304s. SIGMET twice an hour at 82 KB is about 4 MB/day before the local FIR filter throws the rest away; keep the filtered file, and the raw body for 14 days is about 55 MB if nothing is gzipped. Compress it with the other raw JSON.

**Point products.** The 2,672 byte sample is one point, 72 hours, three wind fields. Scaling by variable count (14/3 surface, 20/3 upper air, 14/3 marine) and by the caps (8, 8, and 4 points) and by four runs is about **1.2 MB/day** of JSON. That scale was not measured as a single response. Fourteen days of it is under 20 MB.

## Disk

Recommended retention from `design.md`.

| Store | 14 days, naive raw | Recommended |
| --- | ---: | ---: |
| Open-Meteo grid JSON | 23 MB × 14 ≈ 0.32 GB | same, 14 days |
| Point and marine JSON | ~1.2 MB × 14 ≈ 0.02 GB | 14 days |
| SIGMET raw, uncompressed | 4 MB × 14 ≈ 0.06 GB | 14 days, compressed with the other raw JSON |
| Ensemble raw | ~14 MB | 14 days |
| Bureau tarballs, WA only | 141 MB × 14 ≈ 2.0 GB | **3 days ≈ 0.42 GB** |
| Bureau tarballs, NSW if the same size | another 2.0 GB | **3 days ≈ 0.42 GB** |
| Charts, warnings, METAR/TAF | under 0.1 GB | 14 days |
| Float16 grids, 14 days of lead times | see below | 14 days, then thin |
| Obs SQLite, 1 year | | about 1 MB × 2 states × 365 ≈ **0.7 GB** upper order-of-magnitude |

Float16, ten fields, 1,836 points × 2 bytes × 10 = 36.7 KB per valid time. Sixteen lead times (48 h at the native 3 h step) are about **0.59 MB per run**, about **1.2 MB/day** at two runs, about **16 MB** for 14 days. The thinner archive keeps only MSLP and 850 hPa, one pair per valid time at 3 h, overwritten by the newest run: 8 × 365 × 7.3 KB ≈ **21 MB/year**. The 14-day grid JSON (about 0.32 GB) is the larger grid store.

**Recommended steady state: about 2 GB**, mostly two states of tarballs kept for 3 days plus a year of obs and a year of thin grids. Call it **5 GB** with slack for warning days and a second copy of the last prognosis.

Keeping every half-hour tarball for 14 days, both states, is about **4 GB** of overlap and is not worth it. ECMWF open-data GRIB is about 500 MB a run. Keeping that raw for 14 days would be about 14 GB, so the daemon keeps it for **48 hours** and keeps the normalised float16 grids for 14 days. A hard cap stops the store at **8 GB**. A fetch does not start when the volume has under **20 GB** free. Partial files older than an hour are deleted, and the launchd log is rotated.

## Money

| Option | Cost |
| --- | --- |
| This Mac, launchd, free APIs | No host fee. Bureau FTP and Open-Meteo free tiers. |
| Open-Meteo Standard | €29/month or €319/year. Drops ensemble. Does not unlock a 0.25° national grid by itself (1M calls/month; the 0.25° sketch is ~3.4M). |
| Open-Meteo Professional | €99/month or €1,099/year, 5M calls, ensemble included. The 10M product starts at €160/month. |
| GitHub Actions | Public standard runners free. Private GitHub Free lists 2,000 minutes; Linux 2-core past that is $0.006/minute. A bad fit for half-hourly pulls and for Bureau files (see `design.md`). |
| Small VPS | Price not verified on 26 Sep 2026. |

v0 stays on the free tier, at about 40% of the Open-Meteo daily and monthly caps. The first spend worth considering is a VPS once sleep misses a run the owner wanted, or disk for GRIB in v1, and the price gets checked that day.
