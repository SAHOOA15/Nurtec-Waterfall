# Nurtec QoQ NBRx & TRx Waterfall

Streamlit-in-Snowflake app that decomposes Nurtec's LAAD market-share change quarter-over-quarter into three levers (Written Demand / Rejections / Reversals) with a payer-level breakdown, and rescales the result to NPA (real-world) market share.

## Layout

- `streamlit_app.py` — app entrypoint, page layout, CSS injection
- `src/`
  - `data.py` — Snowflake queries + slicing helpers
  - `waterfall.py` — lever decomposition math (matches the source Excel workbook)
  - `charts.py` — Altair waterfall + payer-breakdown renderers, themed to the PC-HUB design language
  - `qc.py` — Excel-style calculation walk-through tables
- `.streamlit/config.toml` — theme fallbacks
- `snowflake.yml` — Snowflake CLI project descriptor
- `scripts/` — helper scripts used during initial data load / migrations (not part of the running app)

## Data sources

All data is read from Snowflake at runtime. There is no bundled CSV and no Dataiku
fallback — extracted LAAD/NPA data is licensed and is not kept in this repo.

- LAAD (quarterly, long-format): `<DB>.<SCHEMA>.<..._SQL_NURTEC_WATERFALL_QTR_METRICS>`
- NPA market share (quarterly, pre-computed): `<DB>.<SCHEMA>.<..._SQL_NURTEC_WATERFALL_NPA_MARKET_SHARE_SF>`
- NPA data watermark: `<DB>.<SCHEMA>.<..._SQL_NPA_MAX_DATE_SF>`

The NPA table carries one row per `(QTR, BRAND, TYPE)` with `MARKET_SHARE` stored as a
fraction (0–1). `TYPE` holds true `NBRx` and `TRx` grains — NBRx is not approximated
from NRx.

## Quarter completeness

A quarter counts as complete only once its end date falls on or before the NPA
watermark. In-progress quarters stay selectable but are kept out of the default
comparison and are flagged in every section, so a partial quarter cannot quietly
become the default view. If the watermark cannot be read the app assumes every
quarter is complete and simply omits the notice.

## Deploy

Files are deployed to Snowflake via `snow streamlit deploy` (or by uploading through Snowsight Workspaces). The container runtime is Python 3.11. The app expects to run inside Streamlit-in-Snowflake, where it picks up the active session; `st.connection("snowflake")` is used as a fallback.

## Waterfall convention

Sign convention: `impact = actual_MS - ideal_MS` (positive = the lever helped Nurtec's share). Fill-rate is modeled additively as `FR = 1 - RJ - RV`. Payer-level sub-impacts are scaled so they sum to the overall lever impact. See the in-app "How to use this waterfall?" and "What is the 'Other reasons' Category?" expanders for the full explanation.
