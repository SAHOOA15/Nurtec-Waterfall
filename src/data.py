"""Data loading for the Nurtec waterfall app.

Loading strategy
----------------
Each source tries two loaders and uses the first that works:

  1. **Dataiku Dataset API** (`dataiku.Dataset(name).get_dataframe()`). This is
     the live path when the app runs as a Dataiku webapp, which is how it is
     hosted. Dataiku manages dataset refresh from Snowflake on a schedule, so
     the app always reads the latest build. Matches the pattern used by the
     sibling Primary_Care_Webapp and Rebate-Decision-Agent apps.
  2. **Snowflake** via the active Snowpark session (Streamlit-in-Snowflake) or
     `st.connection("snowflake")`. Keeps the app runnable outside Dataiku.

Dataiku dataset names are the Snowflake table name minus the schema prefix, and
each can be overridden with an env var for testing.

There is deliberately no bundled-CSV loader: extracted LAAD/NPA data is
licensed IQVIA plus competitive brand detail and must not live in the repo.

Two sources
-----------
1. LAAD quarter metrics -- long-format with columns
     QTR, CLAIM_TYPE, BRAND, PAYER, METRIC, VALUE.
   Reshaped to a nested dict keyed by BRAND -> PAYER -> METRIC -> VALUE.

2. NPA quarterly market share -- pre-computed upstream, one row per
   (QTR, BRAND, TYPE). Used only to rescale the LAAD waterfall into NPA
   (real-world) market-share space.

Conventions that matter
-----------------------
* `NPA_MS` is a FRACTION (0-1). The source column `MARKET_SHARE` is already
  stored that way, so no scaling is applied here. Callers multiply by 100 for
  display.
* `TYPE` in the NPA source is mixed-case ('TRx' / 'NBRx') while the app passes
  'TRX' / 'NBRX'. All comparisons are case-insensitive.
* NBRx is **true NBRx**, not an NRx proxy -- the upstream table carries both
  grains explicitly.
* Quarter completeness comes from the NPA max-date watermark: a quarter is
  complete only once its end date is on or before the watermark.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

_SCHEMA = "VAW_AMER_DESIGN.USPRIMARYCAREADHOCANALYTICSPARTC"
_PREFIX = "USPRIMARYCAREADHOCANALYTICSPARTC"

# Snowflake fallback targets.
LAAD_TABLE = f"{_SCHEMA}.{_PREFIX}_SQL_NURTEC_WATERFALL_QTR_METRICS"
NPA_MS_TABLE = f"{_SCHEMA}.{_PREFIX}_SQL_NURTEC_WATERFALL_NPA_MARKET_SHARE_SF"
MAX_DATE_TABLE = f"{_SCHEMA}.{_PREFIX}_SQL_NPA_MAX_DATE_SF"

# Dataiku dataset names (primary path): Snowflake table minus the schema prefix.
DKU_LAAD_DATASET = os.getenv("DKU_LAAD_DATASET", "SQL_NURTEC_WATERFALL_QTR_METRICS")
DKU_NPA_MS_DATASET = os.getenv(
    "DKU_NPA_MS_DATASET", "SQL_NURTEC_WATERFALL_NPA_MARKET_SHARE_SF"
)
DKU_MAX_DATE_DATASET = os.getenv("DKU_MAX_DATE_DATASET", "SQL_NPA_MAX_DATE_SF")

# The app's radio passes these.
_VALID_CLAIM_TYPES = ("TRX", "NBRX")


def _try_dataiku(dataset_name: str | None) -> pd.DataFrame | None:
    """Load a Dataiku Dataset by name.

    Returns None when Dataiku isn't importable (not running as a Dataiku
    webapp) or the dataset isn't present, so the caller can fall through to
    Snowflake.
    """
    if not dataset_name:
        return None
    try:
        import dataiku  # type: ignore
    except Exception:
        return None
    try:
        return dataiku.Dataset(dataset_name).get_dataframe()
    except Exception:
        return None


def _get_session():
    """Return a Snowpark session for the Snowflake fallback path.

    Prefers the active Streamlit-in-Snowflake session, then `st.connection`.
    """
    try:
        from snowflake.snowpark.context import get_active_session

        return get_active_session()
    except Exception:
        conn = st.connection("snowflake", ttl=os.getenv("SNOWFLAKE_CONNECTION_TTL"))
        return conn.session()


def _unavailable(label: str, dataset: str, table: str, err: Exception) -> RuntimeError:
    """Build an error that names both routes, so a missing Dataiku dataset is
    immediately diagnosable rather than looking like a Snowflake outage."""
    return RuntimeError(
        f"Could not load {label}. Tried the Dataiku dataset '{dataset}' "
        f"(not found or Dataiku unavailable), then Snowflake table '{table}', "
        f"which failed with: {err}"
    )


@st.cache_data(ttl=3600, show_spinner="Loading LAAD waterfall metrics...")
def load_laad() -> pd.DataFrame:
    df = _try_dataiku(DKU_LAAD_DATASET)
    if df is None:
        try:
            session = _get_session()
            df = session.sql(
                f"SELECT QTR, CLAIM_TYPE, BRAND, PAYER, METRIC, VALUE FROM {LAAD_TABLE}"
            ).to_pandas()
        except Exception as e:
            raise _unavailable(
                "LAAD waterfall metrics", DKU_LAAD_DATASET, LAAD_TABLE, e
            ) from e
    df.columns = [c.upper() for c in df.columns]
    return df


@st.cache_data(ttl=3600, show_spinner="Loading NPA market share...")
def _load_npa_ms() -> pd.DataFrame:
    """Full NPA market-share table: one row per (QTR, BRAND, TYPE).

    Loaded whole (tens of rows) and filtered in pandas, so the Dataiku and
    Snowflake paths share one filter implementation.
    """
    df = _try_dataiku(DKU_NPA_MS_DATASET)
    if df is None:
        try:
            session = _get_session()
            df = session.sql(
                f"SELECT QTR, BRAND, TYPE, MARKET_SHARE FROM {NPA_MS_TABLE}"
            ).to_pandas()
        except Exception as e:
            raise _unavailable(
                "NPA market share", DKU_NPA_MS_DATASET, NPA_MS_TABLE, e
            ) from e
    df.columns = [c.upper() for c in df.columns]
    return df


def npa_market_share_by_qtr(claim_type: str) -> pd.DataFrame:
    """Quarterly Nurtec NPA market share.

    Returns columns QTR and NPA_MS, where NPA_MS is a fraction (0-1).
    """
    ct = claim_type.upper()
    if ct not in _VALID_CLAIM_TYPES:
        raise ValueError(
            f"Unsupported claim_type {claim_type!r}; expected one of {_VALID_CLAIM_TYPES}"
        )

    df = _load_npa_ms()
    sub = df[
        (df["BRAND"].astype(str).str.upper() == "NURTEC")
        & (df["TYPE"].astype(str).str.upper() == ct)
    ]
    out = sub[["QTR", "MARKET_SHARE"]].rename(columns={"MARKET_SHARE": "NPA_MS"})
    return out.sort_values("QTR").reset_index(drop=True)


# ---------- Quarter completeness ----------------------------------------


@st.cache_data(ttl=3600, show_spinner=False)
def npa_max_date() -> pd.Timestamp | None:
    """How far the NPA data extends.

    Returns None if the watermark can't be read, so a missing marker costs the
    completeness callout rather than the whole app. Read positionally because
    the single date column can be named differently across the two sources.
    """
    try:
        df = _try_dataiku(DKU_MAX_DATE_DATASET)
        if df is None:
            session = _get_session()
            df = session.sql(f"SELECT MAX_DT FROM {MAX_DATE_TABLE}").to_pandas()
        if df is None or df.empty:
            return None
        val = df.iloc[0, 0]
        if pd.isna(val):
            return None
        return pd.Timestamp(val)
    except Exception:
        return None


def quarter_end(qtr: str) -> pd.Timestamp | None:
    """'2026Q3' -> Timestamp('2026-09-30'). None if unparseable."""
    try:
        return pd.Period(qtr, freq="Q").end_time.normalize()
    except Exception:
        return None


def incomplete_quarters(qtrs: list) -> set:
    """Quarters whose end date falls after the NPA watermark.

    An unreadable watermark or an unparseable quarter yields "assume complete",
    so this never hides data on the strength of a failed lookup.
    """
    max_dt = npa_max_date()
    if max_dt is None:
        return set()
    out = set()
    for q in qtrs:
        qe = quarter_end(q)
        if qe is not None and qe > max_dt:
            out.add(q)
    return out


def latest_complete_quarter(qtrs: list) -> str | None:
    """Newest quarter in `qtrs` that is not partial, else the newest overall."""
    if not qtrs:
        return None
    partial = incomplete_quarters(qtrs)
    complete = [q for q in sorted(qtrs) if q not in partial]
    return complete[-1] if complete else sorted(qtrs)[-1]


# ---------- Reshaping ---------------------------------------------------


def available_quarters(df: pd.DataFrame, claim_type: str) -> list:
    q = df[df["CLAIM_TYPE"].str.upper() == claim_type.upper()]["QTR"].unique().tolist()
    return sorted(q)


def slice_metrics(df: pd.DataFrame, qtr: str, claim_type: str) -> dict:
    """Return nested dict metrics[BRAND][PAYER][METRIC] = value.

    The source table only carries BRAND in ('Nurtec', 'Abbvie') -- the workbook's
    "Oral CGRP" market total is the sum of the two brands. We synthesize a
    BRAND='Overall' row here so the waterfall math (which reads market-total WD
    and PD_CLAIMS at Overall/payer and Overall/Overall) has values to work with.
    """
    sub = df[(df["QTR"] == qtr) & (df["CLAIM_TYPE"].str.upper() == claim_type.upper())]
    out: dict = {}
    for _, r in sub.iterrows():
        out.setdefault(r["BRAND"], {}).setdefault(r["PAYER"], {})[r["METRIC"]] = r["VALUE"]

    market: dict = {}
    for brand, payers in out.items():
        if brand == "Overall":
            continue
        for payer, metrics in payers.items():
            m = market.setdefault(payer, {})
            for metric in ("WD", "PD_CLAIMS"):
                v = metrics.get(metric)
                if v is not None:
                    m[metric] = m.get(metric, 0.0) + float(v)
    if market:
        out["Overall"] = market
    return out
