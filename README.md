# Nurtec QoQ NBRx & TRx Waterfall

Streamlit app, hosted as a **Dataiku webapp**, that decomposes Nurtec's LAAD market-share change quarter-over-quarter into three levers (Written Demand / Rejections / Reversals) with a payer-level breakdown, and rescales the result to NPA (real-world) market share.

## Layout

- `streamlit_app.py` — app entrypoint, page layout, CSS injection
- `src/`
  - `data.py` — data loading (Dataiku datasets, Snowflake fallback) + slicing helpers
  - `waterfall.py` — lever decomposition math (matches the source Excel workbook)
  - `charts.py` — Altair waterfall + payer-breakdown renderers, themed to the PC-HUB design language
  - `qc.py` — Excel-style calculation walk-through tables
- `.streamlit/config.toml` — theme fallbacks
- `snowflake.yml` — Snowflake CLI project descriptor
- `scripts/` — helper scripts used during initial data load / migrations (not part of the running app)

## Data sources

```
Snowflake            Dataiku                  Streamlit
(source tables)  ->  (managed datasets,   ->  (this app, hosted as
                      scheduled refresh)       a Dataiku webapp)
```

`src/data.py` tries two loaders in order and uses the first that works:

1. **Dataiku Dataset API** — `dataiku.Dataset(name).get_dataframe()`. The live path
   when hosted on Dataiku. Dataiku refreshes these from Snowflake on a schedule.
2. **Snowflake** — active Snowpark session, else `st.connection("snowflake")`. Keeps
   the app runnable outside Dataiku.

There is deliberately **no bundled CSV** — extracted LAAD/NPA data is licensed IQVIA
plus competitive brand detail and must not live in this repo. `data/*.csv` is gitignored.

| Purpose | Dataiku dataset | Snowflake table |
|---|---|---|
| LAAD quarter metrics | `SQL_NURTEC_WATERFALL_QTR_METRICS` | `<DB>.<SCHEMA>.<..._SQL_NURTEC_WATERFALL_QTR_METRICS>` |
| NPA quarterly market share | `SQL_NURTEC_WATERFALL_NPA_MARKET_SHARE_SF` | `<DB>.<SCHEMA>.<..._SQL_NURTEC_WATERFALL_NPA_MARKET_SHARE_SF>` |
| NPA data watermark | `SQL_NPA_MAX_DATE_SF` | `<DB>.<SCHEMA>.<..._SQL_NPA_MAX_DATE_SF>` |

Dataiku dataset names are the Snowflake table name minus the schema prefix. Each can be
overridden for testing via `DKU_LAAD_DATASET`, `DKU_NPA_MS_DATASET`, `DKU_MAX_DATE_DATASET`.

The NPA table carries one row per `(QTR, BRAND, TYPE)` with `MARKET_SHARE` stored as a
fraction (0–1) — do not rescale it. `TYPE` holds true `NBRx` and `TRx` grains (mixed case),
so NBRx is **not** approximated from NRx.

If a load fails, the error names both the Dataiku dataset and the Snowflake table it tried,
so a dataset that has not been published yet is obvious rather than looking like an outage.

## Quarter completeness

A quarter counts as complete only once its end date falls on or before the NPA
watermark. In-progress quarters stay selectable but are kept out of the default
comparison and are flagged in every section, so a partial quarter cannot quietly
become the default view. If the watermark cannot be read the app assumes every
quarter is complete and simply omits the notice.

## Deploy

Primary target is a **Dataiku Streamlit webapp**: the datasets listed above must exist in
the Dataiku project, and the app code is published through the Dataiku webapp. `dataiku`
and `streamlit` are provided by the platform, so they are not pinned in `requirements.txt`.

The repo also still carries `snowflake.yml` for Streamlit-in-Snowflake deployment via
`snow streamlit deploy`. Note its `artifacts:` list is incomplete (it omits `src/`), so it
would need fixing before that path works.

## Waterfall convention

Sign convention: `impact = actual_MS - ideal_MS` (positive = the lever helped Nurtec's share). Fill-rate is modeled additively as `FR = 1 - RJ - RV`. Payer-level sub-impacts are scaled so they sum to the overall lever impact. See the in-app "How to use this waterfall?" and "What is the 'Other reasons' Category?" expanders for the full explanation.
