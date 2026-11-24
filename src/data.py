"""Data loading for the Nurtec waterfall app.

All data comes from Snowflake. There is no bundled-CSV or Dataiku fallback --
extracted LAAD/NPA data is licensed and must not live in the repo.

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
* The NPA source stores `TYPE` mixed-case ('TRx' / 'NBRx') while the app passes
  'TRX' / 'NBRX', hence the case-insensitive comparison.
* Quarter completeness is derived from the NPA max-date watermark: a quarter is
  complete only once its end date is on or before the watermark. The LAAD and
  NPA tables are built in the same upstream run, so one watermark gates both.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

_SCHEMA = "VAW_AMER_DESIGN.USPRIMARYCAREADHOCANALYTICSPARTC"
_PREFIX = "USPRIMARYCAREADHOCANALYTICSPARTC"

LAAD_TABLE = f"{_SCHEMA}.{_PREFIX}_SQL_NURTEC_WATERFALL_QTR_METRICS"
NPA_MS_TABLE = f"{_SCHEMA}.{_PREFIX}_SQL_NURTEC_WATERFALL_NPA_MARKET_SHARE_SF"
MAX_DATE_TABLE = f"{_SCHEMA}.{_PREFIX}_SQL_NPA_MAX_DATE_SF"

# The app's radio passes these; used as an allowlist so claim_type is never
# interpolated into SQL unvalidated.
_VALID_CLAIM_TYPES = ("TRX", "NBRX")


def _get_session():
    """Return a Snowpark session.

    Prefers the active Streamlit-in-Snowflake session. `st.connection` is a
    fallback so a single untested call path isn't the only way in.
    """
    try:
        from snowflake.snowpark.context import get_active_session

        return get_active_session()
    except Exception:
        conn = st.connection("snowflake", ttl=os.getenv("SNOWFLAKE_CONNECTION_TTL"))
        return conn.session()


@st.cache_data(ttl=3600, show_spinner="Loading LAAD waterfall metrics...")
def load_laad() -> pd.DataFrame:
    session = _get_session()
    df = session.sql(
        f"SELECT QTR, CLAIM_TYPE, BRAND, PAYER, METRIC, VALUE FROM {LAAD_TABLE}"
    ).to_pandas()
    df.columns = [c.upper() for c in df.columns]
    return df


@st.cache_data(ttl=3600, show_spinner="Loading NPA market share...")
def npa_market_share_by_qtr(claim_type: str) -> pd.DataFrame:
    """Quarterly Nurtec NPA market share, pre-computed upstream.

    Returns columns QTR and NPA_MS, where NPA_MS is a fraction (0-1).

    Unlike the previous implementation, NBRx is true NBRx rather than an NRx
    proxy -- the upstream table carries both grains explicitly.
    """
    ct = claim_type.upper()
    if ct not in _VALID_CLAIM_TYPES:
        raise ValueError(
            f"Unsupported claim_type {claim_type!r}; expected one of {_VALID_CLAIM_TYPES}"
        )

    session = _get_session()
    df = session.sql(
        f"""
        SELECT QTR, MARKET_SHARE AS NPA_MS
        FROM {NPA_MS_TABLE}
        WHERE UPPER(BRAND) = 'NURTEC'
          AND UPPER(TYPE) = '{ct}'
        """
    ).to_pandas()
    df.columns = [c.upper() for c in df.columns]
    return df.sort_values("QTR").reset_index(drop=True)


# ---------- Quarter completeness ----------------------------------------


@st.cache_data(ttl=3600, show_spinner=False)
def npa_max_date() -> pd.Timestamp | None:
    """How far the NPA data extends.

    Returns None if the watermark can't be read, so a missing marker costs the
    completeness callout rather than the whole app.
    """
    try:
        session = _get_session()
        df = session.sql(f"SELECT MAX_DT FROM {MAX_DATE_TABLE}").to_pandas()
        df.columns = [c.upper() for c in df.columns]
        if df.empty or pd.isna(df["MAX_DT"].iloc[0]):
            return None
        return pd.Timestamp(df["MAX_DT"].iloc[0])
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
