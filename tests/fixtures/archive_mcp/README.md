Tiny subsets of the archive contract, captured read-only on 2026-10-08.

The four float16 files contain the unmodified 2×2 cells at 31.5–31.75°S,
115.5–115.75°E from `ecmwf_ifs025`, run `20261008T00Z`, hours 0 and 3,
variables `mslp` and `t850`. Sidecars and manifest were subset to the same
bounds and hours. Total binary payload: 32 bytes. ECMWF Open Data, CC BY 4.0.

Tests copy these files to temporary stores and use the collector's `sidecar`,
`pack_f16`, and observation schema helpers for additional hand-computed fields.
No test consults the owner's archive or any network service.
