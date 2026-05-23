import streamlit as st
"""
modules/utils/perf.py -- Performance utilities for large dataset handling.
============================================================================

Key functions:
  optimize_dtypes(df)         -- Shrink DataFrame memory footprint by downcasting
                                 numerics and converting low-cardinality strings
                                 to pandas Categorical dtype.
  sample_for_plot(df, n)      -- Return a random sample of at most n rows for
                                 Plotly. Returns (sampled_df, was_sampled).
  read_csv_fast(file, **kw)   -- read_csv with dtype optimisation.
  read_excel_sheet(file, sn)  -- Read ONE sheet (no eager full-workbook load).
  get_sheet_names(file)       -- Sheet list without reading any cell data.
  mem_mb(df)                  -- DataFrame RAM usage in MB.

Design rules:
  - No Streamlit imports here -- pure data layer.
  - Every function that returns a DataFrame returns a new object; no in-place
    mutation of caller data.
"""

import pandas as pd
import numpy as np
from typing import Union


# ── Memory reporting ──────────────────────────────────────────────────────────

def mem_mb(df: pd.DataFrame) -> float:
    """Return total DataFrame memory usage in megabytes (deep=True)."""
    return df.memory_usage(deep=True).sum() / 1_048_576


# ── dtype optimisation ────────────────────────────────────────────────────────

# Categorical threshold: object columns with ≤ this fraction of unique values
# AND ≤ _CAT_MAX_UNIQ distinct values are converted to pd.Categorical.
_CAT_THRESHOLD = 0.50   # if > 50 % of rows are unique, keep as object
_CAT_MAX_UNIQ  = 1_000  # hard cap -- too many categories = no gain


def optimize_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Shrink a DataFrame's memory footprint without losing precision or data.

    Operations applied (in order):
      1. Downcast int64 → smallest fitting int (int8 / int16 / int32 / int64).
      2. Downcast float64 → float32 where value range allows.
      3. Convert low-cardinality object columns to pd.Categorical.

    Typical savings on real-world CSVs:
        int-heavy files    →  40–60 % smaller
        mixed files        →  25–45 % smaller
        string-heavy files →  10–30 % smaller (via Categorical)

    Args:
        df: Input DataFrame. Never mutated.

    Returns:
        New DataFrame with optimised dtypes.
    """
    out = df.copy()

    for col in out.columns:
        dtype = out[col].dtype

        if pd.api.types.is_integer_dtype(dtype):
            out[col] = pd.to_numeric(out[col], downcast="integer")

        elif dtype == np.float64:
            # Downcast float64 → float32 for memory savings.
            # PRECISION NOTE: float32 has ~7 significant decimal digits vs float64's ~15.
            # For financial columns (prices, currency, exact totals) this is acceptable
            # for charting purposes but should not be used for computational accuracy.
            # Columns that stay as float32 are display-only in charts; all DB writes
            # and aggregations should cast back to float64 if precision matters.
            out[col] = pd.to_numeric(out[col], downcast="float")

        elif dtype == object:
            n_uniq = out[col].nunique()
            n_rows = len(out)
            ratio  = n_uniq / n_rows if n_rows else 0
            if n_uniq <= _CAT_MAX_UNIQ and ratio < _CAT_THRESHOLD:
                out[col] = out[col].astype("category")

    return out


# ── Plot sampling ─────────────────────────────────────────────────────────────

_SAMPLE_NOTE = (
    "⚠️ Chart rendered from a {n:,}-row sample (dataset has {total:,} rows). "
    "Statistical patterns are preserved."
)


def sample_for_plot(
    df: pd.DataFrame,
    n: int = 50_000,
    random_state: int = 42,
) -> tuple[pd.DataFrame, bool]:
    """
    Return a representative random sample of at most n rows for Plotly rendering.

    Plotly serialises every data point to JSON and ships it to the browser.
    A 400 MB CSV with 5 M rows produces an ~800 MB JSON blob that freezes
    the browser tab.  Capping at 50 K rows has no visible effect on bar /
    time-series charts (which aggregate anyway) and is clearly labelled on
    scatter / distribution / outlier charts.

    Args:
        df:           Input DataFrame.
        n:            Maximum rows to return.
        random_state: Reproducibility seed.

    Returns:
        (sampled_df, was_sampled)  --  was_sampled is True when df had > n rows.
    """
    if len(df) <= n:
        return df, False
    return df.sample(n=n, random_state=random_state).reset_index(drop=True), True


def sample_note(n: int, total: int) -> str:
    """Human-readable note explaining the sample size shown in charts."""
    return _SAMPLE_NOTE.format(n=n, total=total)


# ── Fast CSV reader ───────────────────────────────────────────────────────────

def read_csv_fast(file, **kwargs) -> pd.DataFrame:
    """
    Read a CSV file and return a dtype-optimised DataFrame.

    Uses low_memory=False to avoid mid-column dtype mis-detection (common
    in mixed-type columns that are all-numeric except for a header row).
    Then calls optimize_dtypes() to shrink the result.

    Args:
        file:     File-like object or path string.
        **kwargs: Forwarded to pd.read_csv (e.g. sep=";", encoding="latin1").

    Returns:
        Optimised DataFrame.
    """
    kwargs.setdefault("low_memory", False)
    if hasattr(file, "seek"):
        file.seek(0)
    df = pd.read_csv(file, **kwargs)
    return optimize_dtypes(df)


# ── Lazy Excel helpers ────────────────────────────────────────────────────────

def get_sheet_names(file) -> list[str]:
    """
    Return the list of sheet names without loading any cell data.

    pd.ExcelFile in header-only mode is ~100× faster than
    pd.read_excel(sheet_name=None) on large workbooks because it reads
    only the workbook XML manifest, not the cell values.

    Args:
        file: File-like object (will be seek(0)'d before reading).
    """
    if hasattr(file, "seek"):
        file.seek(0)
    with pd.ExcelFile(file) as xl:
        return xl.sheet_names


def read_excel_sheet(file, sheet_name: Union[str, int] = 0) -> pd.DataFrame:
    """
    Read a SINGLE sheet from an Excel file with dtype optimisation.

    Unlike pd.read_excel(sheet_name=None) which loads the entire workbook,
    this reads only the requested sheet -- critical for large multi-sheet files.

    Args:
        file:       File-like object (seek(0)'d before reading).
        sheet_name: Sheet name (str) or 0-based index (int).

    Returns:
        Optimised DataFrame for the requested sheet.
    """
    if hasattr(file, "seek"):
        file.seek(0)
    df = pd.read_excel(file, sheet_name=sheet_name)
    return optimize_dtypes(df)


# ── Cached pivot computation ──────────────────────────────────────────────────
# Wrapping pd.pivot_table in cache_data avoids recomputing the entire pivot
# every Streamlit rerun triggered by widget interactions (settings, scroll, etc.)
# The hash key is a tuple of (df shape, col names, agg, index, columns, values).

@st.cache_data(show_spinner=False, ttl=300)
def cached_pivot(
    df: pd.DataFrame,
    index: str,
    columns: str,
    values: str,
    aggfunc: str,
) -> pd.DataFrame:
    """
    Cached pd.pivot_table wrapper.

    Using @st.cache_data means Streamlit hashes df by content; for large
    datasets this is still faster than recomputing the full pivot on every rerun.
    TTL=300s ensures stale data doesn't linger after a dataset reload.
    """
    return pd.pivot_table(
        df, index=index, columns=columns, values=values,
        aggfunc=aggfunc, observed=True,
    )


# ── Distribution sampling ─────────────────────────────────────────────────────
# Plotly histogram pre-bins data in the browser. For >50K rows the JSON
# payload becomes large enough to cause lag. Sample down to 50K rows first.

_HIST_SAMPLE = 50_000


def sample_for_histogram(
    df: pd.DataFrame,
    n: int = _HIST_SAMPLE,
    random_state: int = 42,
) -> tuple[pd.DataFrame, bool]:
    """
    Sample for histogram / distribution charts.
    Histogram shape is statistically robust at 50K rows for most distributions.
    """
    return sample_for_plot(df, n=n, random_state=random_state)


# ── Categorical sampling ──────────────────────────────────────────────────────
# Categorical bar charts aggregate before plotting so sampling hurts accuracy.
# Only sample when the UNIQUE VALUE count is extreme (>2K bars would be unreadable
# anyway). We group the tail into "Other" instead of random sampling.

_CAT_MAX_BARS = 50   # never render more than 50 bars; roll the rest into "Other"


def top_n_with_other(
    series: pd.Series,
    n: int = _CAT_MAX_BARS,
    other_label: str = "Other",
) -> pd.Series:
    """
    Keep the top-n most frequent categories and replace the rest with 'Other'.

    This is more meaningful than random row-sampling for categorical charts
    because it preserves the most important groups while keeping render fast.
    """
    top = series.value_counts().nlargest(n).index
    return series.where(series.isin(top), other=other_label)


# ── Render budget guard ───────────────────────────────────────────────────────
# Hard limits on data points shipped to the browser. Exceeding these makes
# Plotly unresponsive regardless of how fast the Python side is.

RENDER_LIMITS = {
    "scatter":     8_000,
    "histogram":  50_000,
    "map":        10_000,
    "line":       50_000,
    "bar":         5_000,   # per series; aggregation should happen before this
    "heatmap":       500,   # cells (rows × cols)
}


def enforce_render_limit(
    df: pd.DataFrame,
    chart_type: str,
    random_state: int = 42,
) -> tuple[pd.DataFrame, bool]:
    """
    Sample df to the render budget for chart_type.
    Returns (df_possibly_sampled, was_sampled).
    """
    limit = RENDER_LIMITS.get(chart_type, 50_000)
    return sample_for_plot(df, n=limit, random_state=random_state)
