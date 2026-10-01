# Source registry

Checked from Perth on 26 September 2026, about 04:30–04:52 UTC, with small GETs. Latencies are that path, not a promise. Where a policy page did not state a number, this file says so. Phase 1b, the pilot panel and the kite spots, was checked the same day about 05:30–05:52 UTC.

Isobar today (see the app README) shows Bureau MSLP analysis `IDY00050` beside prognosis `IDG00073` / `IDG00074`, pins Perth coastal and Sydney, and reads capital-city observation JSON plus `api.weather.bom.gov.au` for place search and warnings. No API key. This registry is for a daemon that can feed that app without breaking the publishers’ terms.

## How to read a row

Value is for this owner: a synoptic picture (isobars, 850 hPa temperature, rain, ensemble confidence at two points, station obs, METAR/TAF, upper wind, and marine at a few points) centred on Perth and the kite spots at Cottesloe and Safety Bay, with Sydney in view. Radar and satellite stay later. 5 is “use in v0”. A source can score high and still be barred.

## Open-Meteo

Operator: OpenMeteo GmbH, Bürglen, Switzerland. One free non-commercial quota covers the hosts below. Paid plans are a different contract.

| | |
| --- | --- |
| Forecast | `https://api.open-meteo.com/v1/forecast` and `https://api.open-meteo.com/v1/ecmwf` |
| Ensemble | `https://ensemble-api.open-meteo.com/v1/ensemble` |
| Marine | `https://marine-api.open-meteo.com/v1/marine` |
| Run clock | `https://{api\|ensemble-api\|marine-api}.open-meteo.com/data/{model}/static/meta.json` |
| Terms | <https://open-meteo.com/en/terms> |
| Licence | <https://open-meteo.com/en/licence> |
| Call maths | <https://open-meteo.com/en/pricing> (page script, fetched 26 Sep 2026) |
| Docs | <https://open-meteo.com/en/docs/ecmwf-api>, [ensemble](https://open-meteo.com/en/docs/ensemble-api), [GFS](https://open-meteo.com/en/docs/gfs-api), [ICON](https://open-meteo.com/en/docs/dwd-api), [marine](https://open-meteo.com/en/docs/marine-weather-api), [JMA](https://open-meteo.com/en/docs/jma-api), [BOM](https://open-meteo.com/en/docs/bom-api) |

**Free-tier limits, quoted.** Terms, “Non-Commercial Use”: “Less than 10'000 API calls per day, 5'000 per hour and 600 per minute.” Non-commercial only. They may block an application or IP without notice. The pricing table adds a monthly mark of 300,000 calls on the free column. The same page says the free API “carries no uptime guarantee” and points status at <https://status.open-meteo.com> (not fetched). Servers are described as in Europe and North America.

**What one call is.** The pricing page’s calculator (script `nodes/82.BLSaUyDS.js` on 26 Sep 2026) is:

```text
weight = max(1, (variables × models / 10) × max(1, days / 14)) × locations
```

Defaults on that page (10 variables, 14 days, 1 model, 1 location) cost 1.0. The FAQ’s examples match: 15 variables and 2 weeks = 1.5; 15 variables and 4 weeks = 3.0. Ensemble members are not a factor in that script. Several locations in one HTTP request still multiply. A request under the thresholds still costs 1 per location, because of the `max(1, …)`.

**Paid, if the free quota is outgrown.** Stripe pricing table embedded on the pricing page, 26 Sep 2026, amounts in euro cents: API Standard €29/month or €319/year; API Professional €99/month or €1,099/year; “API Professional (10 million calls)” from €160/month. The HTML comparison table says Standard is 1M calls/month and does **not** include the ensemble API; Professional is 5M calls/month and does. Ensemble on the free tier is the one that includes it. “API Enterprise (50 million API calls)” is listed without a price in the payload read here.

**Licence, quoted.** Licence page: API data under CC BY 4.0. “You must include a link next to any location Open-Meteo data are displayed”, example text “Weather data by Open-Meteo.com” to `https://open-meteo.com/`. Upstream centres are listed there separately (ECMWF CC BY, NOAA, DWD CC BY, JMA, BoM CC BY, Copernicus Marine, and others). The server source is AGPLv3; a client that only calls the API is not running that server.

**Conditional requests.** Forecast and ensemble JSON returned no `ETag` or `Last-Modified`. `If-None-Match` and `If-Modified-Since` were ignored (HTTP 200). `meta.json` did both: `ETag: "1790382123-658"` on IFS 9 km, and the same tag returned **304**. Schedule off `meta.json`, not off the forecast body.

**Latency.** About 1.1–1.3 s per small JSON from this Mac. `generationtime_ms` in the bodies was under 2 ms, so the second is the trip to Europe.

### ECMWF IFS and AIFS via Open-Meteo — value 5

Example, Perth coastal grid point, nearest 0.25° cell, no downscaling:

```text
https://api.open-meteo.com/v1/forecast?latitude=-32.00&longitude=115.75&hourly=pressure_msl,temperature_850hPa&forecast_hours=1&models=ecmwf_ifs025&cell_selection=nearest&elevation=nan
```

Live: HTTP 200, grid point −32.00, 115.75, 1024.0 hPa and 4.0 °C at 850 hPa for 2026-09-26T04:00Z. Two locations in one URL returned a JSON list (691 bytes). Sixteen locations, one hour, two variables: HTTP 200, 5,638 bytes, 1.16 s.

| Model id | What came back | Grid |
| --- | --- | --- |
| `ecmwf_ifs025` | MSLP and 850 hPa temperature filled | 0.25° (docs and the snapped point) |
| `ecmwf_ifs` | 9 km IFS HRES, O1280. Point −32, 115.75 snapped to −32.02109, 115.72979 | Reduced Gaussian, docs: native 9 km |
| `ecmwf_aifs025_single` | Filled. 24 h of 2 m temperature and MSLP, all 24 hours non-null | 0.25°. Docs: 6-hourly native, API interpolates |
| `ecmwf_aifs025` | HTTP 200 and **null** 2 m temperature and MSLP | Do not use this id for surface fields |
| `ecmwf_ifs025_ensemble` | Control plus `member01`–`member50` (51 series) | 0.25° |
| `ecmwf_aifs025_ensemble` | Same shape, 51 series, values filled (16.2 / 16.4 °C on the sample) | Snapped to −32.0, 115.75 |

Docs (<https://open-meteo.com/en/docs/ecmwf-api>): IFS runs every 6 hours; open-data 0.25° is 3- and 6-hourly with “an additional delay of 2 hours” against real-time dissemination; 9 km is hourly for 90 h, 3-hourly after, 6-hourly after 144 h, “without any additional delay”; AIFS time steps are 6-hourly; forecasts advertised up to 15 days. Ensemble page: IFS 0.25° ensemble global, 0.25°, 3-hourly, **51 members**, 15 days, every 6 hours; AIFS 0.25° ensemble 6-hourly, 51 members, 15 days, every 6 hours. Pressure levels in both APIs include 850 hPa.

`meta.json` at 04:40Z on 26 Sep 2026, previous run still the latest for ECMWF (00Z was not out yet, which matches a ~6 h delay):

| Model | Init | Available on the API | Delay | Step | `data_end_time` |
| --- | --- | --- | --- | --- | --- |
| `ecmwf_ifs` | 25 Sep 18Z | 26 Sep 00:22Z | 6.4 h | 1 h | 1 Oct 19:00Z (~145 h) |
| `ecmwf_ifs025` | 25 Sep 18Z | 26 Sep 01:13Z | 7.2 h | 3 h | 1 Oct 21:00Z |
| `ecmwf_aifs025_single` | 25 Sep 18Z | 25 Sep 23:41Z | 5.7 h | 6 h | 11 Oct 00:00Z |
| `ecmwf_ifs025_ensemble` | 25 Sep 18Z | 26 Sep 03:29Z | 9.5 h | 3 h | 1 Oct 21:00Z |
| `ecmwf_aifs025_ensemble` | 25 Sep 18Z | 26 Sep 02:55Z | 8.9 h | 6 h | 11 Oct 00:00Z |

A `forecast_days=15` MSLP request the same hour was non-null through 10 Oct 12Z (9 km, 9,600 bytes) and 10 Oct 14Z (0.25°). That is past `data_end_time`. Treat hours beyond `data_end_time` as possibly the previous run’s tail until a later meta says otherwise. The model-updates page says to wait 10 minutes after `last_run_availability_time` because copies across their servers lag.

**Payload.** One point, 24 h, four variables (`pressure_msl`, `temperature_850hPa`, `precipitation`, `wind_speed_10m`), IFS 0.25°: 1,333 bytes. One point, 15 days, MSLP only: ~9.6 KB. Ensemble, one point, 7 days, MSLP, all members: 66,222 bytes, 2.1 s.

**Why 5.** This is the sustainable way to get an Australian MSLP and 850 hPa grid plus ensemble spread without downloading GRIB. It is point sampling, not a native grid download. National 0.25° does not fit the free quota (see `budget.md`).

### GFS and GEFS via Open-Meteo — value 3

Docs: GFS global 0.11° (~13 km) hourly, 16 days, every 6 h; pressure variables on the 0.25° grid; GEFS 0.25° 31 members, 3-hourly, 10 days, every 6 h; GEFS 0.5° 31 members, 35 days.

Live model ids: `ncep_gfs013`, `ncep_gfs025`, `ncep_gfs_seamless` (a `gfs_seamless` alias also returned 200). Ensemble: `ncep_gefs025` returned a control plus 30 member series (31), grid −32.0, 115.75, MSLP 1023.8 hPa. `gfs025` on the ensemble host also returned 200. `gfs_global` returned a null temperature and no members.

`meta.json`: GFS 0.11° 18Z available 23:38Z (5.6 h); GFS 0.25° available 00:54Z (6.9 h); GEFS 0.25° available 23:42Z (5.7 h). GEFS 0.5° (`ncep_gefs05`) at the same moment still showed init 25 Sep 00Z, available 26 Sep 04:19Z. One snapshot; do not treat that as the published cadence (the ensemble page says every 6 h).

Same licence, quota, and lack of conditional GET as the rest of Open-Meteo. Useful as an independent check on the IFS isobars, not as the chart the owner learned on.

### ICON via Open-Meteo — value 2

Docs: ICON global 0.1°, hourly then 3-hourly after 78 h, 7.5 days, every 6 h. ICON-EPS global 26 km, 40 members, 7.5 days, every 12 h. Europe nests do not cover Western Australia (`icon_eu` and `icon_d2` returned “No data is available for this location”).

`dwd_icon` meta: 26 Sep 00Z available 03:46Z (3.8 h). `dwd_icon_eps` update interval 12 h, 00Z available 03:43Z. `icon_global` on the ensemble API returned 40 member keys. Same Open-Meteo rules. A short global, so a poor substitute for a 4-day prognosis.

### JMA GSM via Open-Meteo — value 2

Docs: GSM global 0.5°, 6-hourly, 11 days, every 6 h, and “Due to data licence restrictions only a limited version of data is available.” MSM does not cover Australia. `jma_gsm` meta: 18Z available 03:32Z (9.5 h), 6 h steps. Same Open-Meteo quota. Coarse. Himawari imagery is a different feed, below.

### Marine via Open-Meteo — value 5 at a few points

```text
https://marine-api.open-meteo.com/v1/marine?latitude=-31.96&longitude=115.78&current=wave_height,wave_direction,wave_period
```

HTTP 200, snapped to −31.958336, 115.79167, wave height 1.16 m, direction 224°, period 12 s. Docs: ECMWF WAM 9 km hourly, 15 days, every 6 h (archive from Nov 2025); WAM 0.25° 3-hourly; GFS Wave 0.25° and a 0.16° nest from 52.5°N to 15°S, so the 0.16° nest does **not** cover Perth or Sydney. Model ids in the status script: `ecmwf_wam`, `ecmwf_wam025`, `ncep_gfswave025`, `ncep_gfswave016`. Meta delays on the 18Z run: WAM 5.9 h, WAM 0.25° 7.0 h, GFS Wave about 5.3 h.

Phase 1b used the same host for the kite spots. The marine docs page, fetched 26 Sep 2026, says `cell_selection` defaults to land, and “sea prefers grid-cells on sea.” It also says “The default Best Match provides the best forecast for any given location worldwide.” A request that omits `models` and sets `cell_selection=sea` is the one that filled water temperature. Pinning a named wave model dropped it: at −32.00, 115.70, `ecmwf_wam`, `ecmwf_wam025`, and `ncep_gfswave025` each returned a wave height and `sea_surface_temperature` with unit `undefined` and value null. The unpinned request snapped to −31.958336, 115.70836 and returned wave height 1.48 m, direction 223°, period 11.8 s, swell 1.3 m from 236° at 11.85 s, wind-wave 0.6 m, secondary swell 0.26 m, and sea-surface temperature 18.9 °C. The same point, 72 hours, `wave_height`, `swell_wave_height`, and `sea_surface_temperature`: 72 non-null hours, ending 2026-09-29T04:00Z at 0.9 m, 0.78 m, and 19.2 °C.

`swell_wave_peak_period` was null and `undefined` on that first multi-field call. Do not request it. Secondary swell did fill on Best Match. The docs add: “Secondary swell components are only available for some models. Tertiary components are only available for the GFS wave models.” Tertiary swell is not in the v0 list.

One request with the fourteen fields below, two locations, `forecast_hours=1`, `cell_selection=sea`, no `models`, returned 200 and no nulls. A 72-hour request naming all fourteen was not made. The 72-hour sample is the three fields above, and the sea-level sample is 48 hours. The second point, −32.30, 115.73, snapped to −32.291668, 115.70836 (wave 1.84 m, temperature 18.5 °C, sea level −0.07 m). That snap is an offshore cell. It is not the water inside Safety Bay. The product keeps the snapped coordinate.

| Field | Unit on that response |
| --- | --- |
| `wave_height`, `wave_direction`, `wave_period` | m, °, s |
| `swell_wave_height`, `swell_wave_direction`, `swell_wave_period` | m, °, s |
| `wind_wave_height`, `wind_wave_direction`, `wind_wave_period` | m, °, s |
| `secondary_swell_wave_height`, `secondary_swell_wave_direction`, `secondary_swell_wave_period` | m, °, s |
| `sea_surface_temperature` | °C |
| `sea_level_height_msl` | m |

`sea_level_height_msl` is the docs’ “Sea Level Height including tides (above global mean sea level)”. The same page: “Tides and ocean currents are computed at 0.08° (~8 km) resolution using numerical models. Accuracy at coastal areas is limited. This is not suitable for coastal navigation and does not replace your nautical almanac. Use with caution!” At −32.00, 115.70, `cell_selection=sea`, 48 hourly values from 13:00 AWST on 26 Sep were all non-null, from −0.08 m to 0.41 m, with a rise and fall through the evening. That is the tide signal v0 stores. It is not a Bureau tide table. High and low times are the hourly maxima and minima, so the clock time is only as sharp as the hour.

Same Open-Meteo quota and CC BY credit as the forecast API. v0 is at most four points, four runs a day. A marine grid is not in the budget.

### ACCESS-G via Open-Meteo — value 0, do not use

The BOM API page describes ACCESS-G at 0.15°, hourly, 10 days, runs 00/06/12/18Z, model id `bom_access_global`. The ensemble page describes ACCESS-GE at 40 km, 18 members, 10 days, every 6 h. Live `meta.json`: `bom_access_global` init **26 Jun 2025 18Z**, `data_end_time` 30 Jun 2025, file `Last-Modified` 26 May 2026. `bom_access_global_ensemble` is the same June 2025 init. The feed is not a current forecast.

## ECMWF Open Data (GRIB2) — value 4 as the native grid, not v0

Page: <https://www.ecmwf.int/en/forecasts/datasets/open-data> (`Last-Modified` Sat, 26 Sep 2026 02:38:12 GMT). Portal: <https://data.ecmwf.int/forecasts/>. Terms linked from that page: <https://apps.ecmwf.int/datasets/licences/general/>. CC BY 4.0: <https://creativecommons.org/licenses/by/4.0/deed.en>. Client and mirrors: <https://github.com/ecmwf/ecmwf-opendata>.

**Quoted.** “A subset of ECMWF real-time forecast data from the IFS and AIFS models is made available to the public free of charge. Their use is governed by the Creative Commons CC-BY-4.0 licence and the ECMWF Terms of Use. This means that the data may be redistributed and used commercially, subject to appropriate attribution.” “Access to the Open-Data Portal is currently limited to 500 simultaneous connections.” Rolling archive: “the most recent 12 forecast runs”, about 2–3 days, cycles 00, 06, 12, 18 UTC. “IFS data are released at the end of the real-time dissemination schedule. AIFS data are released as soon the data are produced.” Resolution “0.25 degrees … in GRIB2” with CCSDS compression since July 2023. Higher-resolution fields are a paid dissemination agreement, not this portal. The 9 km field Open-Meteo serves is not this 0.25° open-data file.

**Steps, from that page (IFS cycle 50r1, 13 May 2026).** Atmosphere and wave, `stream=oper` / `stream=wave`: 00Z and 12Z, 0 to 144 by 3 h, then 150 to 360 by 6 h. 06Z and 18Z, 0 to 144 by 3 h only. Ensemble `stream=enfo`, `type=pf`, same steps. Pressure levels include 850 hPa and, since 50r1, 10 hPa. AIFS single (`model=aifs-single`): 6-hourly steps to 360 h, all four cycles. Parameters include `msl`, `2t`, `t` on pressure levels.

**Live objects, 18Z 25 Sep, one byte via `Range`.** All three returned 206, `Accept-Ranges: bytes`, `ETag`, `Last-Modified`.

| Object | Bytes | `Last-Modified` |
| --- | --- | --- |
| `…/20260925/18z/ifs/0p25/oper/20260925180000-0h-oper-fc.grib2` | 134,732,912 | 26 Sep 2026 00:27:00 GMT |
| `…/aifs-single/0p25/oper/20260925180000-0h-oper-fc.grib2` | 89,464,370 | 25 Sep 2026 23:45:00 GMT |
| `…/ifs/0p25/enfo/20260925180000-0h-enfo-ef.grib2` | not measured (HEAD 200 only) | |

Portal index HTML was 460 KB and did not list files in the first screen; the paths above were confirmed by the 206 responses. No numeric request-rate besides 500 concurrent connections. No per-byte cap was stated.

**Decoding.** Not timed in this phase. One time step is the whole parameter set, CCSDS GRIB2, so eccodes/cfgrib has to be built and a global 0.25° field is 1,440 × 721 points. Step 0 alone is 129 MB (IFS) or 85 MB (AIFS). A 00Z/12Z run has 85 steps if every step is that size: on the order of 10 GB, and that multiple was **not** measured. This is why v0 samples Open-Meteo and leaves GRIB for a later host with disk and a decoder.

## NOAA GFS on AWS and NOMADS — value 3

| | |
| --- | --- |
| Bucket | `s3://noaa-gfs-bdp-pds` HTTPS `https://noaa-gfs-bdp-pds.s3.amazonaws.com/` |
| Registry | <https://registry.opendata.aws/noaa-gfs-bdp-pds/> |
| NOMADS tree | `https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.YYYYMMDD/HH/atmos/` |
| Filter form | `https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl` (HTTP 200, 3,648 byte form, 26 Sep 2026) |
| NWS disclaimer | <https://www.weather.gov/disclaimer> |

**Licence, quoted** from the disclaimer: “The information on National Weather Service (NWS) Web pages are in the public domain, unless specifically noted otherwise, and may be used without charge for any lawful purpose” provided you do not claim it as your own, imply NOAA/NWS endorsement, or modify it and present it as official. Registry: “NOAA data disseminated through NODD are open to the public and can be used as desired.” No rate limit was found on the registry page or the disclaimer. AWS is the polite path; NOMADS is the operational one and is known to shed load, but that behaviour was not measured here.

**Live, GFS 18Z 25 Sep.** Listing `gfs.20260925/18/atmos/` shows both huge NetCDF (`gfs.t18z.atmf000.nc` about 7.0 GB in the listing) and GRIB. Do not pull the NetCDF for a chart.

`gfs.t18z.pgrb2.0p25.f000`: **484,604,024 bytes**, `Last-Modified` Fri, 25 Sep 2026 21:36:01 GMT (about 3.6 h after 18Z), `ETag` present, `Accept-Ranges`. The `.idx` is 31,814 bytes. First record:

```text
1:0:d=2026092518:PRMSL:mean sea level:anl:
2:998608:d=2026092518:CLMR:1 hybrid level:anl:
```

So mean-sea-level pressure in that file is about 976 KB, and a client can `Range` it. A full 0.25° GRIB is 462 MB per forecast hour. Same decoding warning as ECMWF. Filter CGI was not used to download a grid.

Cadence from the registry: four cycles a day, 00/06/12/18Z. The public 0.25° GRIB is the field Open-Meteo’s `ncep_gfs025` already decodes. Direct GRIB is for v1 if Open-Meteo is down or a true grid is required.

## Bureau of Meteorology

Two documents, and they are not the same.

**Copyright** <https://www.bom.gov.au/copyright> (canonical; `article:modified_time` Fri, 17/07/2026). Default when no other terms are stated: “you can download, copy and use our content for personal use, or use within your organisation. You must not supply it to any other person or use it for any commercial purpose.” Unauthorised use includes anything that “disrupts or otherwise interferes with our site, products and/or services. This means that we do not allow you to use automated or manual techniques to hack, scrape or otherwise extract material from our site.” They may block an IP without notice. Restricted content: “Some of our content – such as radar images, high-resolution maps, and other specialised data – may be subject to a data licence agreement.” “You need a data licence agreement to: access the material; reproduce or publish it in any form.” Attribution if not CC BY: “Reproduced with the permission of the Bureau of Meteorology” or “© Bureau of Meteorology”. CC BY text, when that licence is the one on the page: “Bureau of Meteorology, © Commonwealth of Australia. Licensed from the Commonwealth of Australia under a Creative Commons Attribution 4.0 International licence.” Disclaimer <https://www.bom.gov.au/disclaimer>: no warranty, liability excluded as far as the law allows.

**Data services** <https://www.bom.gov.au/resources/data-services>: “Some forecast, warning and observation text products are free. These are not for commercial use. Extensive datasets are available through paid subscription services.” Real-time grids, satellite, and radar are described as a **paid** registered-user service. Licence PDF linked there: <https://www.bom.gov.au/sites/default/files/2026-07/bureau-of-meteorology-data-licence-agreement-june-2026.pdf> (not parsed). Registered users: <http://reg.bom.gov.au/reguser/reguser.shtml>, contact `webreg@bom.gov.au`.

**Anonymous FTP, the channel that allows automation.** Catalogue <https://reg.bom.gov.au/catalogue/data-feeds.shtml> (page says last updated 23 February 2026) and user guide *Anonymous FTP Service*, version 1.0, updated 4 February 2026 (<https://reg.bom.gov.au/catalogue/Bureau_of_Meteorology_Anonymous_FTP_Service_user_guide.pdf>): “The Bureau of Meteorology makes a number of real-time forecast, warning and observation products and analysis charts available via the web and Anonymous FTP. These products are free to access and are not for commercial use.” “Unless otherwise stated, products available via the anonymous FTP service are subject to the default terms of the Bureau’s copyright notice” (personal or in-organisation use; do not supply to anyone else; no commercial use). “Please consider the update frequency of products when downloading.” No requests-per-minute figure is in that guide. Host `ftp://ftp.bom.gov.au/anon/gen/`. Login anonymous. Availability is not guaranteed.

That is the path a daemon may use. Polling `www.bom.gov.au` on a timer is the scrape the July 2026 copyright notice forbids. Isobar’s existing in-app fetch can stay as it is; the daemon should not add a second website poller.

### Observation JSON — value 5, via FTP bundle

Half-hourly guide, version 3.2, 17 June 2024 (<https://reg.bom.gov.au/catalogue/72_hr_historical_obs.pdf>): product bundle `IDB00011`, files `ftp://ftp.bom.gov.au/anon/gen/fwo/IDX60910.tgz`, also the per-station names `IDX60910.NNNNN.json`. “Every half hour, at approximately x:05 and x:35.” NSW file includes the ACT. X is D/N/Q/S/T/V/W.

Live website objects (these confirm the product and the station; they are not the planned pull):

| Product path | Station | WMO | Notes |
| --- | --- | --- | --- |
| `…/IDW60901/IDW60901.94614.json` | Swanbourne | 94614 | Capital-city. 112,565 bytes. `Last-Modified` 26 Sep 2026 04:31:40 GMT for obs `20260926043000`. `ETag` present. Repeat with `If-None-Match` returned **304**. |
| `…/IDW60801/IDW60801.94614.json` | Swanbourne | 94614 | Header product name “Weather Observations”. |
| `…/IDW60910/IDW60910.94614.json` | Swanbourne | 94614 | 72-hour product. Full body came back as 158,709 bytes (Range ignored). |
| `…/IDN60901/IDN60901.94768.json` | Sydney - Observatory Hill | 94768 | `ETag` size field 0x1afcc = 110,540. Same etag layout matched the Swanbourne byte count. |
| `…/IDN60901/IDN60901.94767.json` | Sydney Airport | 94767 | 0x1bbeb = 113,643 |
| `…/IDV60901/IDV60901.95936.json` | Melbourne (Olympic Park) | 95936 | |
| `…/IDQ60901/IDQ60901.94576.json` | Brisbane | 94576 | |
| `…/IDS60901/IDS60901.94648.json` | Adelaide (West Terrace / ngayirdapira) | 94648 | 119,207 bytes. `IDS60901.94672` was **404**. |
| `…/IDT60901/IDT60901.94970.json` | Hobart | 94970 | |
| `…/IDD60901/IDD60901.94120.json` | Darwin Airport | 94120 | |
| `…/IDN60903/IDN60903.94926.json` | Canberra | 94926 | HTTP 200 on that path. JSON header `ID` was `IDN60901`, `main_ID` `IDN60902`. |

Anonymous FTP, verified with `SIZE`/`LIST` headers only:

| URL | Bytes | `Last-Modified` |
| --- | --- | --- |
| `ftp://ftp.bom.gov.au/anon/gen/fwo/IDW60910.tgz` | 2,943,378 | 26 Sep 2026 04:35:01 GMT |

The tarball was not opened, so this file does not claim a member list. The guide’s naming and the matching web object are why WA v0 uses it. NSW `IDN60910.tgz` was not size-checked. Per-station `IDW60910.94614.json` and `IDW60901.94614.json` were **not** on the FTP path tried (curl 78). The website JSON supports conditional GET; FTP gives `Last-Modified` and size.

Fields the popover wants are in the capital-city JSON: `air_temp`, `wind_dir`, `wind_spd_kmh`, `gust_kmh`, `press_msl`, `press_tend`. Swanbourne’s latest row had `press_msl: null` at 12:30 pm WST. The daemon cannot invent a pressure the station did not send. Sydney Observatory Hill is the city obs; YSSY is the airport METAR. Keep both.

### Warnings XML — value 5

Website sample, which Isobar’s red dot needs in substance:

```text
https://www.bom.gov.au/fwo/IDW20100.xml
```

HTTP 200, `Content-Length: 7254`, `Last-Modified` 26 Sep 2026 01:17:58 GMT, `ETag` present. Bureau product XML, schema `http://www.bom.gov.au/schema/v1.7/product.xsd`, identifier `IDW20100`, issue `2026-09-26T01:17:50Z`, next routine `2026-09-26T05:01:00Z`, title “Marine Wind Warning”. `IDZ00059.warnings.xml`, `IDZ00054.warnings.xml`, and `IDZ00059.cap.xml` were **404**. A CAP feed was not found. The catalogue says anonymous FTP lists warning products and that the national warnings page refreshes when a warning is issued. The FTP guide puts warnings under `/fwo`. A daemon should follow the FTP product list for the WA and NSW warning ids, on the order of the next-routine time in the file (here about 4 h) plus a check more often during weather, and only download when `Last-Modified` moves. Do not poll the website.

### MSLP charts — value 5 for `IDG*`, mixed for `IDY00050`

| URL | Where | Bytes | `Last-Modified` |
| --- | --- | --- | --- |
| `https://www.bom.gov.au/fwo/IDG00073.pdf` and `ftp://ftp.bom.gov.au/anon/gen/fwo/IDG00073.pdf` | web and FTP | 621,172 | 26 Sep 2026 04:13:24 GMT |
| `https://www.bom.gov.au/fwo/IDG00074.gif` and the same path on FTP | web and FTP | 147,087 | 26 Sep 2026 04:13:24 GMT |
| `https://www.bom.gov.au/fwo/IDY00050.pdf` | web only | 176,006 | 26 Sep 2026 01:35:05 GMT |
| `ftp://ftp.bom.gov.au/anon/gen/fwo/IDY00050.pdf` and `.gif` | | not found | |
| `ftp://ftp.bom.gov.au/anon/gen/difacs/IDX0002.gif` | FTP, guide’s “Manual MSLP charts” | 136,091 | 25 Sep 2026 18:39:39 GMT |

`IDG00073` / `IDG00074` are on the anonymous FTP service, so a personal non-commercial mirror is inside that guide, with Bureau attribution, and the bytes must not be handed to anyone else. `IDY00050` was not on the FTP paths tried. Leave that file to Isobar’s existing fetch. `IDX0002` is a different manual chart and was many hours older. One timestamp is not a cadence. Isobar already treats chart refresh as 30 minutes in the app; that is the app’s poll, not a Bureau issue time. The FTP guide’s only instruction is to match the product’s own update frequency.

### Radar `IDR703` — value 5 as a picture, do not automate

`ftp://ftp.bom.gov.au/anon/gen/radar/IDR703.gif`: 26,812 bytes, `Last-Modified` 26 Sep 2026 04:45:10 GMT. The website GIF the same hour was 26,734 bytes at 04:40:07 GMT. Catalogue and FTP guide: images issued every five minutes. Radar guide version 1.3, 5 November 2025: products are “available to Registered Users”, and “Files are also available via anonymous FTP” subject to the copyright notice. Naming `IDRnnn3` is the 128 km local image; `IDR703.gif` is the latest frame. Perth’s Serpentine radar is the `703` in that name; the guide’s `nnn` is the radar id.

The July 2026 copyright page then says radar images are the example of content that needs a data licence to access and to reproduce. The data-services page lists radar under the paid real-time service. The February 2026 FTP guide still lists `/radar`. Those documents disagree. **v0 does not download Bureau radar.** Ask `webreg@bom.gov.au` before any later version does. RainViewer is the substitute that was checked.

### `api.weather.bom.gov.au` — do not use

Live copyright string on every body read, including errors:

> This application programming interface (API) is owned by the Bureau of Meteorology. You must not use, copy or share it.

That is an explicit ban. A small check on 26 September 2026 confirmed that location search and warnings responded, with no `ETag`. Isobar uses this API for search and warnings. The daemon must not. Place search can stay inside the app. Warnings for the daemon come from FTP XML.

### Bureau satellite JPGs — do not automate

The FTP guide lists Himawari-9 JPG and GeoTIFF under `ftp://ftp.bom.gov.au/anon/gen/gms/` (`IDE`), every ten minutes per the catalogue. The data-services page puts satellite data on the paid service, and the copyright page’s “high-resolution maps, and other specialised data” clause is the same family as radar. Not checked file-by-file. Not in the plan. Use NOAA/JMA Himawari or GIBS, which publish their own terms.

## aviationweather.gov — value 5

Docs: <https://aviationweather.gov/data/api/> (`Last-Modified` Wed, 16 Sep 2026 19:59:35 GMT). “This service facilitates machine-to-machine access.”

**Limits, quoted.** “Please keep requests limited in scope and frequency. Maximum results per query apply as well as rate limiting against frequent requests.” “Set a custom user agent to prevent automated filtering inadvertently blocking valid traffic.” “most METARs update once per hour.” “Wait between consecutive requests — maximum 100 requests per minute. Exceeding request limits will result in access being blocked.” “All requests are rate limited to 100 requests per minute. Most endpoints return a maximum of 400 entries.” Cache files exist for bulk. Database window “up to the previous 30 days.” No licence paragraph was on that page. The NWS disclaimer’s public-domain terms are the nearest statement found, and they are about weather.gov pages; this file does not pretend the API page repeated them.

```text
https://aviationweather.gov/api/data/metar?ids=YPPH,YSSY&format=json
https://aviationweather.gov/api/data/taf?ids=YPPH,YSSY&format=json
https://aviationweather.gov/api/data/isigmet?format=json
https://aviationweather.gov/api/data/pirep?id=YPPH&distance=300&format=json
```

| Call | Result |
| --- | --- |
| METAR YPPH, YSSY | 200, 831 bytes, ~0.35 s. YPPH `260430Z 12015KT 9999 SCT035 BKN039 17/09 Q1023`. YSSY `260430Z 04015KT CAVOK 30/12 Q1020`. Weak `ETag`. `If-None-Match` returned **304**. `cache-control: max-age=60`. |
| TAF both | 200, 6,191 bytes. YSSY was an AMD TAF. |
| SIGMET worldwide | 200, `Content-Length: 86095`. No Australian filter was required to prove the endpoint. |
| PIREP | `ids=YPPH` without a distance was 400: “Must specify bounding box or stations ID and radial distance”. `id=YPPH&distance=300` was **204** empty. Docs: PIREPs are “Primarily US and North Atlantic”, so an empty Australian result is expected. |

Formats listed: raw, JSON, GeoJSON, CSV, XML, and IWXXM for METAR/TAF. Pull METAR hourly and TAF a few times a day, with the weak ETag. Do not pull the worldwide SIGMET on a tight loop; 86 KB is fine a few times an hour if the daemon filters to YMMM/YBBB client-side.

Phase 1b, same endpoints, 05:30–05:40Z. METAR YPPH and YSSY again, 831 bytes: YPPH `260530Z 14013KT 9999 SCT033 SCT040 16/09 Q1022`, YSSY `260530Z 03015KT CAVOK 28/13 Q1019`. JSON `wdir`/`wspd` were 140°/13 kt and 30°/15 kt. JSON `clouds` for YPPH were 3300 ft and 4000 ft, both `SCT`. YSSY `clouds` was empty and `cover` was `CAVOK`. JSON `visib` was the string `6+` for both, which is the US statute-mile rendering of `9999`. The Australian visibility is the raw group, in metres. The help page <https://aviationweather.gov/help/data/> says the wind group “is coded in tens of degrees relative to true north using three figures.” `fltCat` came back `VFR`. That is a US category. The product does not keep it.

`/api/data/isigmet?format=json` was 104 features, 81,545 bytes. Five had `firId` YMMM and three YBBB. The text `AIRMET` did not occur. One YMMM feature was `WSAU21 YMMC 260405`, hazard `TURB`, `SEV`, base 26,000 ft, top 34,000 ft, `firName` “YMMM MELBOURNE”. The API page’s HTML, Last-Modified still Wed, 16 Sep 2026, does not contain the string `isigmet`. The table rows that do appear call G-AIRMETs “Contiguous 48 United States” and AIRMETs “Alaska”, and say “CONUS text AIRMETs were discontinued in January 2025.” `/api/data/airmet?format=json` returned 56 features whose `region` values were two-letter ids (`AB`–`AL`, `FC`–`FK`, `JB`–`JF`), with no Australian FIR. `/api/data/airsigmet` and `/api/data/sigmet` each returned five US convective features and no YMMM or YBBB.

User-chosen aerodromes are extra ICAO ids on the same METAR and TAF URLs, one request for the whole list. v0 caps the list at eight, including YPPH and YSSY. That is still one HTTP request an hour, against the 100-per-minute limit.

Australian AIRMET and the Bureau’s grid-point wind page are not this API. They are recorded under the refusals below.

## RainViewer — value 4, the radar substitute

Docs: <https://www.rainviewer.com/api.html> (`Last-Modified` Sat, 26 Sep 2026 02:42:41 GMT).

```text
https://api.rainviewer.com/public/weather-maps.json
https://tilecache.rainviewer.com/v2/radar/c529dba5664f/256/2/3/2/2/1_1.png
```

Maps JSON: 766 bytes, ~1.1 s, `generated` 1790397928, host `https://tilecache.rainviewer.com`, past frames about every 10 minutes, `nowcast` empty in this response. Tile: HTTP 200, 4,084 bytes, 256×256 RGBA PNG, ~1.0 s.

**Terms, quoted.** “This API is available for personal and educational use only.” “The API is free for personal or educational use.” “There are no hard rate limits posted, but please cache aggressively and avoid hammering endpoints — abuse may result in your IP being blocked. We don't publish an SLA.” Attribution: “You must display credit such as ‘Weather data by RainViewer’ with a link back to rainviewer.com”. They also ask for `https://www.rainviewer.com/` “on your website”. “You can change any image received by this API without any restrictions.” Data can disappear when a radar owner stops sharing. Personal menu-bar use fits. Do not build a public radar tile service on it.

Maps JSON carried an ETag. A tile was not revalidated. Cache the maps document and only fetch tiles for the frames Isobar will show (Perth and Sydney), not the world pyramid.

## NASA GIBS — value 3 for Himawari pictures

Docs: <https://nasa-gibs.github.io/gibs-api-docs/> (`Last-Modified` Mon, 21 Sep 2026 20:30:39 GMT). WMTS REST. The introduction names a “Data Use Guidance and Acknowledgements” section. `…/data-use-guidance/` and `…/introduction/` returned **404** on 26 Sep 2026, and the HTML that did load did not contain a licence sentence. **No GIBS licence is claimed here.**

Live tile, Himawari AHI band 13:

```text
https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/Himawari_AHI_Band13_Clean_Infrared/default/2026-09-26/GoogleMapsCompatible_Level6/3/5/2.jpg
```

HTTP 200, `Content-Type: image/png`, 334 bytes, 256×256. That tile is a valid image; it is not a useful Australian scene at that z/x/y. Endpoint and layer id work. No `ETag` was recorded. Cadence was not measured. GIBS is a visualisation, not a calibrated grid.

IMERG was not confirmed. This request returned 400 `InvalidParameterValue` / `TILEMATRIXSET`:

```text
https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/IMERG_Precipitation_Rate/default/2026-09-25/GoogleMapsCompatible_Level5/5/4/3.png
```

Do not schedule IMERG until the layer id and matrix set are read from the GIBS layer list.

## JMA / NOAA Himawari-9 — value 3, too heavy for v0

Registry: <https://registry.opendata.aws/noaa-himawari/>. Bucket `s3://noaa-himawari9`. JMA instrument notes linked from the registry: <https://www.data.jma.go.jp/mscweb/en/himawari89/space_segment/spsg_ahi.html>.

**Licence, quoted** from the registry: “Himawari data is produced and managed by JMA. NOAA has rights to distribute this data freely and openly to the public.” “NOAA and JMA request attribution for the use or dissemination of unaltered data.” Do not imply endorsement. If you modify it, do not present it as unaltered. Update frequency stated there: full disk 10 minutes; target areas 2.5 minutes; two areas 0.5 minutes. No rate limit on that page. A JMA licence page beyond this registry text was not fetched.

Live prefix `AHI-L1b-FLDK/2026/09/26/0430/` listed objects with `Last-Modified` 2026-09-26T04:40:33Z, so the 0430 UTC scan was up about 10 minutes later. Two band-1 segments: 3,988,280 bytes and 7,725,779 bytes, `ETag` present. A full disk is many segments and many bands. That is a satellite processor, not a menu-bar pull. v0 should use a GIBS tile if a picture is wanted, after the GIBS acknowledgement page is actually found.

## Upper air, surface points, runways, stations

These are the phase 1b feeds. Call weight is the Open-Meteo formula already quoted. Days at or under 14 do not raise it. A null field with unit `undefined` still counts if it is named in the request, so those names are left out.

### `ecmwf_ifs` surface points — value 5

One hour at −32.00, 115.75, `models=ecmwf_ifs`, `cell_selection=nearest`, `elevation=nan`, `wind_speed_unit=kn`, snapped to −32.02109, 115.72979, elevation 0. HTTP 200, about 1.2 s. Filled: temperature 17.5 °C, dew point 8.9 °C, MSLP 1023.1 hPa, wind 15.6 kn from 119°, gust 21.2 kn, precipitation 0.00 mm, CAPE 10 J/kg, visibility 52,320 m, cloud cover 77%, low cloud 74%, weather code 2. `daily=sunrise,sunset` for that cell on 26 Sep was 2026-09-25T22:01Z and 2026-09-26T10:15Z (06:01 and 18:15 AWST).

The same model, 72 hours of wind speed, direction, and gust at the Cottesloe coordinate below, was hourly and non-null from 2026-09-26T05:00Z through 2026-09-29T04:00Z. Pressure-level names on this model, including `freezing_level_height` and `temperature_850hPa`, came back null with unit `undefined`. Upper air does not use `ecmwf_ifs`.

Five coordinates in one URL, `elevation=nan` repeated once each, `forecast_hours=1`, each snapped to a different 9 km cell:

| Request | Snap |
| --- | --- |
| Cottesloe −31.9953964, 115.7511955 | −32.02109, 115.72979 |
| Rottnest −32.0, 115.5 | −32.02109, 115.512665 |
| Perth Airport −31.9403, 115.967003 | −31.95079, 115.915665 |
| Safety Bay −32.3040595, 115.7286309 | −32.302284, 115.74545 |
| Garden Island −32.2, 115.7 | −32.231987, 115.71429 |

YSSY (−33.946, 151.177, from the OurAirports airport row) was not in that five-point call. It is not the same cell as Perth.

### `ecmwf_ifs025` winds and temperature aloft — value 5

−31.94, 115.97 snapped to −32.0, 116.0. `wind_speed_unit=kn`. One hour, 2026-09-26T05:00Z:

| hPa | °C | kn | direction ° | height m |
| --- | ---: | ---: | ---: | ---: |
| 1000 | 15.0 | | | 192 |
| 925 | 8.3 | | | 844 |
| 850 | 4.5 | 19.4 | 91 | 1537 |
| 700 | −4.0 | 4.0 | 95 | 3096 |
| 500 | −22.4 | 15.4 | 285 | 5670 |
| 300 | −42.5 | 55.7 | 306 | 9248 |

Wind is requested at 850, 700, 500, and 300 only. Temperature and geopotential height are also requested at 1000 and 925 so a day with a sub-zero 850 hPa temperature still has a warmer level underneath. `freezing_level_height` was null and `undefined` here, as on `ecmwf_ifs`. `ncep_gfs025` returned 2260 m for that name at the same cell and hour. v0 does not add GFS to obtain it. The stored freezing level is a linear interpolation of temperature against the geopotential heights that bracket 0 °C. On this hour that is 4.5 °C at 1537 m and −4.0 °C at 3096 m, which is 2362 m. The GFS figure is a one-off cross-check, not a feed.

`cloud_base` on `ecmwf_ifs025` was null and `undefined`. CAPE on this cell was 20 J/kg. The grid and the surface points carry CAPE. A CAPE number is not a thunderstorm warning. The warning remains the Bureau XML, plus any SIGMET whose hazard is `TS` inside YMMM or YBBB.

Bureau grid-point wind and temperature is linked from <https://www.bom.gov.au/aviation/> as `/aviation/charts/grid-point-forecasts/`. The anonymous-FTP catalogue page, fetched again the same day (55,281 bytes), had no text match for GPWT, AIRMET, SIGMET, METAR, TAF, or tide. The daemon does not poll the chart page. The 0.25° point above is the upper-air feed.

### Coastal stations — value 5, already in the WA bundle

`IDW60801.{wmo}.json` on the website, first rows only, Last-Modified 26 Sep 2026 about 05:24Z. Confirmation, not the pull. The pull stays `IDW60910.tgz` on anonymous FTP.

| WMO | Name on the header | Lat, lon | Latest row |
| --- | --- | --- | --- |
| 94602 | Rottnest Island | −32.0, 115.5 | 05:00Z, ESE 13 kt, gust 19 |
| 94614 | Swanbourne | −32.0, 115.8 | 05:00Z, ESE 12 kt, gust 22 |
| 95607 | Garden Island | −32.2, 115.7 | 05:00Z, SE 15 kt, gust 18 |
| 95605 | Hillarys Point Boat Harbour | −31.8, 115.7 | 05:00Z, E 7 kt, gust 19 |
| 94608 | Perth | −31.9, 115.9 | 05:00Z, ESE 8 kt, gust 16 |
| 94151 | Perth Airport | −31.9, 116.0 | 05:07Z, ESE 17 kt, gust 27, `cloud_base_m` 990, `vis_km` 10 |

`IDW60801.94610.json` was **404**. Perth Airport in this product is 94151. YPPH is still the METAR. Keep both. Rottnest, Swanbourne, Garden Island, and Hillarys had `cloud_base_m` null, `vis_km` “-”, and swell fields null. Nothing is invented in their place.

Ocean Reef is not a row that was found. <https://www.bom.gov.au/wa/observations/perth.shtml>, fetched the same hour, names Rottnest, Swanbourne, Garden Island, and Hillarys, and the page text does not contain “Ocean Reef” or “Reef”. The rainfall list `IDCJMC0014`, <https://www.bom.gov.au/climate/data/lists_by_element/alphaWA_136.txt>, produced 25 Sep 2026, 2,897 lines, has no line containing “reef”. No WMO id is stated here for Ocean Reef. Hillarys, 95605, is the coastal station on that observations page nearest the suburb.

### Runway true headings — value 5, OurAirports

<https://ourairports.com/data/>, fetched 26 Sep 2026: “All data is released to the Public Domain, and comes with no guarantee of accuracy or fitness for use.” Credit is requested and “you’re not required to.” `runways.csv` is linked to <https://davidmegginson.github.io/ourairports-data/runways.csv>, 3,966,309 bytes, `Last-Modified` Sat, 26 Sep 2026 01:54:01 GMT. The data dictionary, <https://ourairports.com/help/data-dictionary.html>, says `le_heading_degT` is degrees true, not magnetic.

Rows for the two default aerodromes, `closed=0`:

| ICAO | Ends | True headings | Length ft |
| --- | --- | --- | --- |
| YPPH | 03/21 | 14 / 194 | 11,299 |
| YPPH | 06/24 | 59 / 239 | 7,096 |
| YSSY | 07/25 | 74 / 254 | 8,300 |
| YSSY | 16L/34R | 168 / 348 | 7,999 |
| YSSY | 16R/34L | 168 / 348 | 12,999 |

The HTML runway pages name those ends and do not print the true headings. The CSV is the source. v0 reads it when the aerodrome list changes, or when a weekly `Last-Modified` check moves, and keeps only the configured ICAO ids. No rate limit is stated on the data page. A weekly check is the schedule. Adding an aerodrome means filtering the same file, up to the cap of eight.

METAR `wdir` and these headings are both degrees true, so the crosswind angle does not apply magnetic variation a second time. The METAR direction is in tens of degrees. The component inherits that rounding.

### Shore normal — measured once, not a feed

Nominatim, one lookup, returned Cottesloe Beach as OSM way 29283079, −31.9953964, 115.7511955, with “Data © OpenStreetMap contributors, ODbL 1.0.” The polygon’s longest side is 108 m on bearing 360°. The shore runs north–south. The seaward side is west. v0’s onshore direction, the direction an onshore wind comes from, is **270°**.

Nominatim returned no beach named Safety Bay. The suburb centre was −32.3040595, 115.7286309. The foreshore polygon used for the bearing is Waikiki Foreshore, way 559982738, −32.3150385, 115.7365707, about 1.5 km south along the same shore. Its long sides lie near 146° and the reciprocal 326°. The length-weighted axis is 144.5°. The westward normal is 234.5°, and v0 stores **235°**. That is the Safety Bay `onshore_from_deg`. A search for “Safety Bay Beach” and a bounded beach search in that box did not return a better polygon. Overpass coastline queries from this Mac returned HTTP 504 and were not used.

The bearings are constants in the spot config, with the way id beside them. The polygon is not stored. The ledger attributes OpenStreetMap. They are not re-fetched on a timer.

### What stays off the pilot panel

**NAIPS and NOTAM.** The NAIPS login page, <https://www.airservicesaustralia.com/naips/Account/LogOn>, fetched 26 Sep 2026, says: “The use of automated tools, including bots, scripts, crawlers, scrapers, artificial intelligence tools or similar technologies, to access, extract, download, harvest, replicate or monitor data from NAIPS or its underlying systems without Airservices Australia’s prior written authorisation is strictly prohibited.” A further sentence requires prior written consent before NAIPS information is used to create a database or a product. NOTAM text lives behind that login. v0 does not call NAIPS and does not scrape NOTAM.

**Australian AIRMET.** The aviation index links `/aviation/warnings/airmet/` and `/aviation/warnings/graphical-airmet/`. Those are Bureau website pages. The FTP catalogue search above did not list them, and the aviationweather endpoints that answered were US or, for `isigmet`, SIGMET rather than AIRMET. No allowed machine feed was found. AIRMET is not in v0.

**Bureau tide tables.** Predictions for Fremantle, Hillarys, Rottnest Island, and others are linked from <https://www.bom.gov.au/australia/tides/>. Conditions: <https://www.bom.gov.au/oceanography/projects/ntc/Tidal_Information_Conditions_of_Use.pdf>, one page, `Last-Modified` Sun, 18 Jan 2026 23:02:38 GMT (PDF metadata created 14 Jan 2026 AWST). Any publication “must acknowledge copyright in the Material in the Commonwealth of Australia represented by the Bureau of Meteorology” and must show a disclaimer that begins “The Bureau of Meteorology gives no representation or warranty of any kind”. A modified product has a second disclaimer on that page. The PDF states no request rate and no permission for a daemon. The FTP catalogue did not list a tide product. The July 2026 copyright notice still forbids scraping the website. Bureau tide pages are not polled. The sea-level series above is the substitute, with the Open-Meteo navigation caution stored next to it.

## Not planned, on terms

| Source | Why it stays out |
| --- | --- |
| `api.weather.bom.gov.au` | Payload: “You must not use, copy or share it.” |
| Bureau radar images, website or FTP | July 2026 copyright: a data licence is required to access and reproduce radar images. FTP guide still lists them. Conflict resolved by not ingesting. |
| Bureau Himawari JPGs on `ftp.bom.gov.au` | Same specialised-data / paid-satellite wording. NOAA/JMA bucket is the open copy. |
| Scraping `www.bom.gov.au` on a timer | Copyright forbids automated scrape or extract. Anonymous FTP is the allowed machine path for the products it actually carries. |
| Open-Meteo ACCESS-G / ACCESS-GE | Meta still on a June 2025 run. |
| `ecmwf_aifs025` (no `_single`) | Surface fields came back null. |
| NAIPS, including NOTAM | Login page prohibits automated extraction without written authorisation. |
| Bureau AIRMET, GPWT, and tide pages | Website products. Not on the FTP catalogue. aviationweather’s AIRMET feed was not Australian. |
| `freezing_level_height` and `cloud_base` on ECMWF via Open-Meteo | Accepted, then null, unit `undefined`. Interpolate freezing level. Ceiling comes from the METAR. |
| Pressure levels on `ecmwf_ifs` (9 km) | Same null/`undefined` result. Upper air is `ecmwf_ifs025`. |
| `models=ecmwf_wam` when water temperature is required | SST came back undefined. Omit `models` and use Best Match. |
