"""
modules/ui/data_cleaner.py -- Advanced data cleaning and text normalisation panel.
===================================================================================

Provides show_data_cleaner(df) — a multi-tab Streamlit panel rendered on the
upload page after the existing Column Manager.

Tabs:
  🔤 Text Clean      Text normalisation, special-char removal, case conversion,
                     strip whitespace, alphanumeric-only filter.
  🔄 Find & Replace  Exact string or regex bulk find/replace across a column.
  ✅ Validate        Check a column against a pattern (email, phone, postcode,
                     integer, decimal, custom regex).  Shows pass/fail counts
                     and highlights failing rows.
  ✂️ String Ops      Trim, pad, extract substring, split into new column,
                     concatenate two columns.
  🔢 Numeric Clean   Clamp to min/max range, fill outliers with median/mean,
                     round to N decimal places, scale to 0-1 / z-score.

Every tab shows a LIVE PREVIEW (first 8 rows before / after) before the
user commits the change. Changes are applied to st.session_state.df in-place
and trigger st.rerun().

Design rules:
  - No auto-apply on widget change — always requires an explicit "Apply" button.
  - Never mutates the original DataFrame passed in; previews work on copies.
  - Fully offline — no network calls.
"""

from __future__ import annotations

import re
import streamlit as st
import pandas as pd
import numpy as np


# ── Performance constants ─────────────────────────────────────────────────────
# Live previews sample at most this many rows so that checkbox/widget changes
# never block the render on large datasets (e.g. 1 M+ rows).  The full data
# is only processed when the user clicks an explicit Apply button.
_PREVIEW_SAMPLE = 2_000


def _sample(series: pd.Series) -> tuple[pd.Series, bool]:
    """Return (sample, was_sampled).  Samples without replacement when large."""
    if len(series) > _PREVIEW_SAMPLE:
        return series.sample(_PREVIEW_SAMPLE, random_state=42), True
    return series, False


def _count_label(n_changed: int, total_sample: int, full_len: int, was_sampled: bool) -> str:
    """Human-readable change-count caption, noting estimated % when sampled."""
    if was_sampled:
        pct = n_changed / total_sample * 100 if total_sample else 0
        est = int(pct / 100 * full_len)
        return (
            f"**Live preview** (sampled {total_sample:,} of {full_len:,} rows) — "
            f"~{pct:.0f}% will change (~{est:,} of {full_len:,} values estimated):"
        )
    return f"**Live preview** — {n_changed:,} of {full_len:,} values will change:"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _preview_table(before: pd.Series, after: pd.Series, n: int = 20) -> pd.DataFrame:
    """Build a Before / After DataFrame showing ONLY rows that match / will change.

    The preview is strictly limited to rows where Before != After so users see
    exactly what will be affected — random unrelated rows are never shown.
    If no rows match, an empty DataFrame is returned and the caller shows a
    'no matches found' message instead.
    """
    changed_mask = before.astype(str) != after.astype(str)
    changed_idx  = [i for i, v in enumerate(changed_mask) if v]
    if not changed_idx:
        return pd.DataFrame({"Before": [], "After": []})
    show_idx = changed_idx[:n]
    return pd.DataFrame({
        "Before": before.iloc[show_idx].astype(str).values,
        "After":  after.iloc[show_idx].astype(str).values,
    })


def _apply_df(df: pd.DataFrame, col: str, new_series: pd.Series) -> None:
    """Commit a transformed series back to session_state.df."""
    df[col] = new_series
    st.session_state.df = df


def _changed_count(before: pd.Series, after: pd.Series) -> int:
    return int((before.astype(str) != after.astype(str)).sum())


# ── Tab: Text Clean ───────────────────────────────────────────────────────────

def _tab_text_clean(df: pd.DataFrame) -> None:
    str_cols = df.select_dtypes(include=["object", "string", "category"]).columns.tolist()
    if not str_cols:
        st.info("No text columns found in the dataset.")
        return

    col = st.selectbox("Column to clean", str_cols, key="dc_tc_col")
    series = df[col].astype(str)

    st.markdown("**Select operations to apply** (applied in order shown):")
    c1, c2, c3 = st.columns(3)
    with c1:
        do_strip    = st.checkbox("Strip leading/trailing whitespace", value=True,  key="dc_tc_strip")
        do_lower    = st.checkbox("Lowercase",                          value=False, key="dc_tc_lower")
        do_upper    = st.checkbox("Uppercase",                          value=False, key="dc_tc_upper")
        do_title    = st.checkbox("Title Case",                         value=False, key="dc_tc_title")
    with c2:
        do_rmspec   = st.checkbox("Remove special characters",          value=False, key="dc_tc_spec")
        do_alphanum = st.checkbox("Alphanumeric only (remove all else)", value=False, key="dc_tc_alnum")
        do_rmdigits = st.checkbox("Remove digits",                      value=False, key="dc_tc_rmdig")
        do_rmspaces = st.checkbox("Collapse multiple spaces → one",     value=False, key="dc_tc_csp")
    with c3:
        do_rmhtml   = st.checkbox("Strip HTML tags",                    value=False, key="dc_tc_html")
        do_unicode  = st.checkbox("Normalise unicode (NFKD → ASCII)",   value=False, key="dc_tc_uni")
        do_rmnl     = st.checkbox("Remove newlines / carriage returns", value=False, key="dc_tc_nl")
        do_empty_na = st.checkbox("Empty string → NaN",                 value=False, key="dc_tc_emna")

    def _apply_ops(s: pd.Series) -> pd.Series:
        s = s.copy()
        if do_strip:    s = s.str.strip()
        if do_lower:    s = s.str.lower()
        if do_upper:    s = s.str.upper()
        if do_title:    s = s.str.title()
        if do_rmhtml:   s = s.str.replace(r"<[^>]+>", "", regex=True)
        if do_unicode:
            import unicodedata
            s = s.apply(lambda v: unicodedata.normalize("NFKD", str(v))
                        .encode("ascii", "ignore").decode("ascii"))
        if do_rmnl:     s = s.str.replace(r"[\r\n\t]", " ", regex=True)
        if do_rmspec:   s = s.str.replace(r"[^a-zA-Z0-9\s]", "", regex=True)
        if do_alphanum: s = s.str.replace(r"[^a-zA-Z0-9]",   "", regex=True)
        if do_rmdigits: s = s.str.replace(r"\d", "", regex=True)
        if do_rmspaces: s = s.str.replace(r"\s{2,}", " ", regex=True).str.strip()
        if do_empty_na: s = s.replace({"": np.nan, "nan": np.nan, "None": np.nan})
        return s

    # ── Sampled live preview — never runs on the full dataset during render ──
    sample, was_sampled = _sample(series)
    preview_sample = _apply_ops(sample)
    n_changed = _changed_count(sample, preview_sample)

    st.caption(_count_label(n_changed, len(sample), len(series), was_sampled))
    preview_df = _preview_table(sample, preview_sample)
    st.dataframe(
        preview_df.style.apply(
            lambda c: ["background-color:rgba(16,185,129,0.12)" if row["Before"] != row["After"] else ""
                       for _, row in preview_df.iterrows()],
            axis=0, subset=["After"],
        ),
        use_container_width=True, hide_index=True,
    )

    if st.button("✅ Apply Text Clean", key="dc_tc_apply", type="primary",
                 disabled=n_changed == 0):
        # Apply to the full dataset only on explicit button click
        _apply_df(df, col, _apply_ops(df[col].astype(str)))
        st.success(f"✅ Cleaned `{col}`.")
        st.rerun()


# ── Tab: Find & Replace ───────────────────────────────────────────────────────

def _tab_find_replace(df: pd.DataFrame) -> None:
    str_cols = df.select_dtypes(include=["object", "string", "category"]).columns.tolist()
    if not str_cols:
        st.info("No text columns found in the dataset.")
        return

    col    = st.selectbox("Column", str_cols, key="dc_fr_col")
    series = df[col].astype(str)

    fa, fb = st.columns(2)
    with fa:
        find_val = st.text_input("Find", placeholder="e.g. N/A  or  ^\\d+$", key="dc_fr_find")
    with fb:
        repl_val = st.text_input("Replace with", placeholder="e.g. (leave blank to delete)", key="dc_fr_repl")

    use_regex   = st.checkbox("Use regular expression", key="dc_fr_regex")
    case_insens = st.checkbox("Case-insensitive match",  key="dc_fr_ci")

    if find_val:
        try:
            flags  = re.IGNORECASE if case_insens else 0
            # Use a sample for the live preview to keep the UI responsive.
            sample, was_sampled = _sample(series)
            if use_regex:
                preview_sample = sample.str.replace(find_val, repl_val, regex=True,  flags=flags)
            else:
                preview_sample = sample.str.replace(find_val, repl_val, regex=False, flags=flags)
            n_changed = _changed_count(sample, preview_sample)

            if n_changed == 0:
                st.info(
                    f"🔍 No values matching **`{find_val}`** found in column **`{col}`**. "
                    "Try adjusting your search term or toggling case-sensitivity."
                )
            else:
                st.caption(_count_label(n_changed, len(sample), len(series), was_sampled))
                preview_df = _preview_table(sample, preview_sample)
                st.dataframe(preview_df, use_container_width=True, hide_index=True)

                if st.button("✅ Apply Find & Replace", key="dc_fr_apply", type="primary"):
                    # Full-data apply only on button click
                    if use_regex:
                        full_result = series.str.replace(find_val, repl_val, regex=True,  flags=flags)
                    else:
                        full_result = series.str.replace(find_val, repl_val, regex=False, flags=flags)
                    _apply_df(df, col, full_result)
                    st.success(f"✅ Replace applied to `{col}` — {n_changed:,} value(s) changed.")
                    st.rerun()
        except re.error as e:
            st.error(f"Invalid regex: {e}")
    else:
        st.info("Enter a search term above to preview changes.")


# ── Tab: Validate ─────────────────────────────────────────────────────────────

_PATTERNS = {
    "Email address":       r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$",
    "Phone (intl. +XX)":  r"^\+?[0-9\s\-\(\)]{7,20}$",
    "UK postcode":         r"^[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2}$",
    "US ZIP code":         r"^\d{5}(-\d{4})?$",
    "Integer number":      r"^-?\d+$",
    "Decimal number":      r"^-?\d+(\.\d+)?$",
    "Date (YYYY-MM-DD)":   r"^\d{4}-\d{2}-\d{2}$",
    "URL (http/https)":    r"^https?://[^\s]+$",
    "UUID":                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    "Custom regex…":       "",
}


def _tab_validate(df: pd.DataFrame) -> None:
    str_cols = df.columns.tolist()
    col = st.selectbox("Column to validate", str_cols, key="dc_val_col")
    series = df[col].dropna().astype(str)

    pattern_name = st.selectbox("Validation rule", list(_PATTERNS.keys()), key="dc_val_pat")
    if pattern_name == "Custom regex…":
        pattern = st.text_input("Custom regex pattern", key="dc_val_custom",
                                placeholder=r"e.g. ^\d{4}-\d{2}-\d{2}$")
    else:
        pattern = _PATTERNS[pattern_name]
        st.code(pattern, language="regex")

    if not pattern:
        return

    try:
        # Run match on the full (non-null) series for accurate counts —
        # validation is lightweight (regex match, no data copy) so this is safe.
        mask_pass  = series.str.match(pattern, case=True, na=False)
        mask_fail  = ~mask_pass
        n_pass = int(mask_pass.sum())
        n_fail = int(mask_fail.sum())
        n_null = int(df[col].isna().sum())
        total  = len(df)

        pa, pb, pc = st.columns(3)
        pa.metric("✅ Pass",    f"{n_pass:,}")
        pb.metric("❌ Fail",    f"{n_fail:,}",  delta=f"{n_fail/total*100:.1f}%" if total else "")
        pc.metric("⬜ Null",    f"{n_null:,}")

        if n_fail:
            st.markdown(f"**Rows that fail validation** (first 20 of {n_fail:,}):")
            fail_idx = series[mask_fail].index[:20]
            st.dataframe(df.loc[fail_idx, [col]].reset_index(),
                         use_container_width=True, hide_index=True)

            action = st.radio("Action on failing rows", [
                "None — just report",
                "Replace failing values with NaN",
                "Flag: add a new boolean column '_valid_<colname>'",
            ], key="dc_val_action")

            if st.button("✅ Apply", key="dc_val_apply", type="primary",
                         disabled=action == "None — just report"):
                if "Replace" in action:
                    new_series = df[col].copy().astype(object)
                    new_series[series[mask_fail].index] = np.nan
                    _apply_df(df, col, new_series)
                    st.success(f"✅ Set {n_fail:,} invalid values to NaN.")
                else:
                    flag_col = f"_valid_{col}"
                    new_flag  = df[col].astype(str).str.match(pattern, case=True, na=False)
                    df[flag_col] = new_flag
                    st.session_state.df = df
                    st.success(f"✅ Added column `{flag_col}` (True = valid).")
                st.rerun()
        else:
            st.success(f"✅ All {n_pass:,} non-null values pass the validation rule.")

    except re.error as e:
        st.error(f"Invalid regex: {e}")


# ── Tab: String Ops ───────────────────────────────────────────────────────────

def _tab_string_ops(df: pd.DataFrame) -> None:
    str_cols = df.select_dtypes(include=["object", "string", "category"]).columns.tolist()
    if not str_cols:
        st.info("No text columns found in the dataset.")
        return

    op = st.selectbox("Operation", [
        "Trim to fixed length",
        "Pad to fixed length (left / right)",
        "Extract substring (start, end positions)",
        "Split column into two (by delimiter)",
        "Concatenate two columns into a new column",
        "Extract with regex capture group",
    ], key="dc_so_op")

    col = st.selectbox("Source column", str_cols, key="dc_so_col")
    series = df[col].astype(str)
    new_series = series.copy()
    new_col_name: str | None = None

    try:
        if op == "Trim to fixed length":
            length = st.number_input("Max characters to keep", 1, 1000, 50, key="dc_so_len")
            side   = st.radio("Keep from", ["Left (start)", "Right (end)"], horizontal=True, key="dc_so_side")
            new_series = series.str[:length] if "Left" in side else series.str[-length:]

        elif op == "Pad to fixed length (left / right)":
            length  = st.number_input("Target length", 1, 200, 10, key="dc_so_padlen")
            char    = st.text_input("Pad character", value="0", max_chars=1, key="dc_so_padchar")
            side    = st.radio("Pad side", ["Left", "Right"], horizontal=True, key="dc_so_padside")
            new_series = (series.str.zfill(length) if side == "Left" and char == "0"
                         else series.str.ljust(length, char) if side == "Right"
                         else series.str.rjust(length, char))

        elif op == "Extract substring (start, end positions)":
            sa, sb = st.columns(2)
            start = sa.number_input("Start (0-based)", 0, 999, 0, key="dc_so_start")
            end   = sb.number_input("End (exclusive, 0 = end of string)", 0, 999, 0, key="dc_so_end")
            new_series = series.str[start: end if end > 0 else None]

        elif op == "Split column into two (by delimiter)":
            delim    = st.text_input("Delimiter", value=",", key="dc_so_delim")
            n_part   = st.radio("Keep which part", ["First part", "Second part"], horizontal=True, key="dc_so_part")
            new_col_name = st.text_input("New column name", value=f"{col}_part", key="dc_so_newcol")
            part_idx = 0 if "First" in n_part else 1
            new_series = series.str.split(delim, n=1, expand=False).apply(
                lambda x: x[part_idx] if isinstance(x, list) and len(x) > part_idx else np.nan
            )

        elif op == "Concatenate two columns into a new column":
            col2     = st.selectbox("Second column", [c for c in df.columns if c != col], key="dc_so_col2")
            sep      = st.text_input("Separator", value=" ", key="dc_so_sep")
            new_col_name = st.text_input("New column name", value=f"{col}_{col2}", key="dc_so_concatcol")
            new_series = series.str.cat(df[col2].astype(str), sep=sep)

        elif op == "Extract with regex capture group":
            pattern  = st.text_input("Regex with one capture group ()", key="dc_so_re",
                                     placeholder=r"e.g. (\d{4}) to extract 4-digit year")
            new_col_name = st.text_input("New column name", value=f"{col}_extracted", key="dc_so_recol")
            if pattern:
                new_series = series.str.extract(f"({pattern.strip('()')})", expand=False)
            else:
                st.info("Enter a regex pattern above.")
                return

        n_changed = _changed_count(series, new_series)
        target_col = new_col_name if new_col_name else col
        label = f"new column `{target_col}`" if new_col_name else f"`{col}`"
        # Show preview on a sample only — avoids blocking large datasets
        sample, was_sampled = _sample(series)
        ns_sample = new_series.iloc[:len(sample)] if not was_sampled else new_series.loc[sample.index]
        st.caption(_count_label(n_changed, len(sample), len(series), was_sampled) +
                   f" → {label}")
        st.dataframe(_preview_table(sample, ns_sample.astype(str)),
                     use_container_width=True, hide_index=True)

        if st.button("✅ Apply", key="dc_so_apply", type="primary"):
            if new_col_name:
                df[new_col_name] = new_series
                st.session_state.df = df
                st.success(f"✅ Created column `{new_col_name}`.")
            else:
                _apply_df(df, col, new_series)
                st.success(f"✅ Applied to `{col}` — {n_changed:,} values updated.")
            st.rerun()

    except Exception as e:
        st.error(f"Error: {e}")


# ── Tab: Numeric Clean ────────────────────────────────────────────────────────

def _tab_numeric_clean(df: pd.DataFrame) -> None:
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if not num_cols:
        st.info("No numeric columns found in the dataset.")
        return

    col    = st.selectbox("Column", num_cols, key="dc_nc_col")
    series = pd.to_numeric(df[col], errors="coerce")

    op = st.selectbox("Operation", [
        "Clamp to min/max range",
        "Fill outliers (IQR method) with median",
        "Fill outliers (IQR method) with mean",
        "Round to N decimal places",
        "Scale to 0–1 (min-max normalisation)",
        "Z-score standardisation (mean=0, std=1)",
        "Fill NaN with median",
        "Fill NaN with mean",
        "Fill NaN with custom value",
    ], key="dc_nc_op")

    new_series = series.copy()

    try:
        if op == "Clamp to min/max range":
            ca, cb = st.columns(2)
            mn = ca.number_input("Min value", value=float(series.min()), key="dc_nc_mn")
            mx = cb.number_input("Max value", value=float(series.max()), key="dc_nc_mx")
            new_series = series.clip(lower=mn, upper=mx)

        elif "outliers" in op:
            iqr_mult = st.slider("IQR multiplier (lower = stricter)", 1.0, 3.0, 1.5, 0.1,
                                 key="dc_nc_iqr")
            Q1, Q3 = series.quantile(0.25), series.quantile(0.75)
            IQR    = Q3 - Q1
            lo, hi = Q1 - iqr_mult * IQR, Q3 + iqr_mult * IQR
            fill   = series.median() if "median" in op else series.mean()
            mask   = (series < lo) | (series > hi)
            n_out  = int(mask.sum())
            st.caption(f"Detected **{n_out:,}** outlier(s) outside [{lo:.2f}, {hi:.2f}]")
            new_series = series.copy()
            new_series[mask] = fill

        elif op == "Round to N decimal places":
            n_dec = st.number_input("Decimal places", 0, 10, 2, key="dc_nc_dec")
            new_series = series.round(int(n_dec))

        elif op == "Scale to 0–1 (min-max normalisation)":
            mn, mx = series.min(), series.max()
            new_series = (series - mn) / (mx - mn) if mx > mn else series * 0

        elif op == "Z-score standardisation (mean=0, std=1)":
            new_series = (series - series.mean()) / series.std()

        elif op == "Fill NaN with median":
            new_series = series.fillna(series.median())

        elif op == "Fill NaN with mean":
            new_series = series.fillna(series.mean())

        elif op == "Fill NaN with custom value":
            fill_val = st.number_input("Fill value", value=0.0, key="dc_nc_fill")
            new_series = series.fillna(fill_val)

        n_changed = int((series.round(8) != new_series.round(8)).sum())
        # Show a sampled preview so large datasets don't stall the render
        sample, was_sampled = _sample(series)
        ns_sample = new_series.loc[sample.index] if was_sampled else new_series
        before_str = sample.astype(str)
        after_str  = ns_sample.round(4).astype(str)
        st.caption(_count_label(n_changed, len(sample), len(series), was_sampled))
        st.dataframe(_preview_table(before_str, after_str),
                     use_container_width=True, hide_index=True)

        if st.button("✅ Apply Numeric Clean", key="dc_nc_apply", type="primary",
                     disabled=n_changed == 0):
            _apply_df(df, col, new_series)
            st.success(f"✅ Applied to `{col}` — {n_changed:,} values updated.")
            st.rerun()

    except Exception as e:
        st.error(f"Error: {e}")


# ── Public entry point ────────────────────────────────────────────────────────

def show_data_cleaner(df: pd.DataFrame) -> pd.DataFrame:
    """
    Render the full Data Cleaning panel.

    Call this from the upload page after show_column_manager() and
    show_dtype_transformer().  Returns the (possibly updated) DataFrame,
    but the canonical source of truth is st.session_state.df — this
    function calls st.rerun() after every mutation.
    """
    st.markdown("---")
    st.markdown("## 🧹 Data Cleaning & Validation")
    st.caption(
        "Clean, validate, and transform column values before running analysis. "
        "Every tab shows a live preview of changes before you apply them."
    )

    tabs = st.tabs([
        "🔤 Text Clean",
        "🔄 Find & Replace",
        "✅ Validate",
        "✂️ String Ops",
        "🔢 Numeric Clean",
    ])

    with tabs[0]: _tab_text_clean(df)
    with tabs[1]: _tab_find_replace(df)
    with tabs[2]: _tab_validate(df)
    with tabs[3]: _tab_string_ops(df)
    with tabs[4]: _tab_numeric_clean(df)

    return st.session_state.get("df", df)
