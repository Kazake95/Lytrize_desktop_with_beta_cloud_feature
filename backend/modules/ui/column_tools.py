"""
modules/ui/column_tools.py -- Column type classification and data transformation UI.
====================================================================================

Provides two main UI components used on the upload page:

  show_column_classifier(df):
      Renders a table where the user confirms or overrides the auto-detected
      type of each column (Numeric / Categorical / Date-Time / Ignore).
      After confirmation, writes the classified lists to session_state:
          st.session_state.num_cols  -- list of confirmed numeric columns
          st.session_state.cat_cols  -- list of confirmed categorical columns
          st.session_state.dt_cols   -- list of confirmed date/time columns
      These are the definitive column lists used by ALL analysis runners.

  show_data_transform_tools(df):
      Optional transformation panel with actions like:
          - Drop a column permanently
          - Rename a column
          - Fill NA values with mean / mode / a custom string
          - Cast a column to a different dtype
      Each action mutates st.session_state.df in-place and calls st.rerun().

CONTRIBUTING -- to add a new transformation tool:
    Add a new tab or expander inside show_data_transform_tools() below the
    existing actions. Follow the same pattern:
        1. UI widgets to configure the action.
        2. A button to confirm it.
        3. Mutate st.session_state.df in-place.
        4. Call st.rerun() to refresh the display.
"""
"""
modules/ui/column_tools.py
Data-type transformer and column classifier widgets shown on the upload page.
"""

import streamlit as st
import numpy as np
import pandas as pd
import datetime


def _preview_conversion(series: pd.Series, new_dtype: str) -> dict:  # Dry-run a dtype cast and return stats + sample — never mutates series.
    """
    Dry-run a dtype conversion and return stats + sample rows.
    Never mutates the original series.

    Returns dict with keys:
        total       -- total rows
        success     -- rows that will convert cleanly
        new_nulls   -- extra nulls introduced (conversion failures)
        pct         -- success % (0-100 float)
        sample_df   -- DataFrame with 'Before' / 'After' columns (up to 8 rows)
        dtype_after -- actual dtype of the converted result
    """
    import datetime

    total = len(series)
    n_null_before = int(series.isna().sum())

    try:
        if new_dtype == "datetime64[ns]":
            converted = pd.to_datetime(series, errors="coerce")

        elif new_dtype == "date":
            converted = pd.to_datetime(series, errors="coerce").dt.date

        elif new_dtype == "time":
            src = series.astype(str).str.strip()
            parsed = pd.to_datetime("1970-01-01 " + src, errors="coerce")
            mask_failed = parsed.isna()
            if mask_failed.any():
                parsed[mask_failed] = pd.to_datetime(src[mask_failed], errors="coerce")
            # Try AM/PM formats for failed rows
            still_failed = parsed.isna()
            if still_failed.any():
                for fmt in ("%I:%M %p", "%I:%M:%S %p", "%I %p"):
                    remaining = still_failed & parsed.isna()
                    if not remaining.any():
                        break
                    parsed[remaining] = pd.to_datetime(
                        "1970-01-01 " + src[remaining], format=f"1970-01-01 {fmt}",
                        errors="coerce"
                    )
            converted = parsed.dt.strftime("%H:%M:%S").where(parsed.notna(), other=None)

        elif new_dtype == "timedelta64[ns]":
            src = series
            if total > 0 and isinstance(series.iloc[0], datetime.time):
                src = series.apply(
                    lambda v: f"{v.hour}:{v.minute:02d}:{v.second:02d}"
                    if isinstance(v, datetime.time) else str(v)
                )
            converted = pd.to_timedelta(src.astype(str), errors="coerce")

        elif new_dtype in ("string", "object"):
            converted = series.astype(str)

        elif new_dtype == "category":
            converted = series.astype("category")

        elif new_dtype == "bool":
            src = series.astype(str).str.strip().str.lower()
            converted = src.map({
                "true": True, "1": True, "yes": True,
                "false": False, "0": False, "no": False,
            })

        elif new_dtype in ("int64", "float64"):
            converted = pd.to_numeric(series, errors="coerce").astype(new_dtype)

        else:
            converted = series.astype(new_dtype)

    except Exception as exc:
        return {"error": str(exc)}

    n_null_after  = int(pd.Series(converted).isna().sum())
    new_nulls     = max(0, n_null_after - n_null_before)
    success       = total - new_nulls
    pct           = round((success / total) * 100, 1) if total else 0.0

    # Build a representative sample: first few rows + any rows that WILL fail
    idx_fail = pd.Series(converted).isna() & series.notna()
    sample_idx = list(range(min(6, total)))
    fail_idx   = [i for i in idx_fail[idx_fail].index[:3] if i not in sample_idx]
    sample_idx = list(dict.fromkeys(sample_idx + fail_idx))[:8]

    before_vals = series.iloc[sample_idx].reset_index(drop=True)
    after_vals  = pd.Series(converted).iloc[sample_idx].reset_index(drop=True)

    sample_df = pd.DataFrame({
        "Before": before_vals.astype(str),
        "After":  after_vals.astype(str).replace("None", "⚠️ null").replace("NaT", "⚠️ null").replace("nan", "⚠️ null"),
    })

    try:
        dtype_after = str(pd.Series(converted).dtype)
    except Exception:
        dtype_after = new_dtype

    return {
        "total":      total,
        "success":    success,
        "new_nulls":  new_nulls,
        "pct":        pct,
        "sample_df":  sample_df,
        "dtype_after": dtype_after,
    }


def show_dtype_transformer(df):  # Optional: drop, rename, fill NA, or cast columns before analysis.
    st.markdown("---")
    st.markdown("## 🔍 Data Type Inspector & Transformer")

    dtype_df = pd.DataFrame({
        "Column": df.columns,
        "Current Dtype": df.dtypes.astype(str),
        "Sample Value": [str(df[col].iloc[0]) if len(df) > 0 else "" for col in df.columns]
    }).reset_index(drop=True)
    st.dataframe(dtype_df, use_container_width=True, hide_index=False)

    st.markdown("### 🛠️ Transform Column Types")
    col_to_convert = st.selectbox("Select column to transform", df.columns, key="dtype_col")
    current_dtype = str(df[col_to_convert].dtype)

    target_options = ["object","string","int64","float64","bool","category",
                      "datetime64[ns]","date","time","timedelta64[ns]"]

    default_idx = target_options.index("object")
    if "int" in current_dtype:      default_idx = target_options.index("int64")
    elif "float" in current_dtype:  default_idx = target_options.index("float64")
    elif "datetime" in current_dtype: default_idx = target_options.index("datetime64[ns]")
    elif "bool" in current_dtype:   default_idx = target_options.index("bool")

    new_dtype = st.selectbox(
        f"Convert '{col_to_convert}' from `{current_dtype}` to:",
        options=target_options, index=default_idx,
        key=f"dtype_target_{col_to_convert}")

    # ── Conversion Preview ────────────────────────────────────────────────────
    prev_key = f"_preview_{col_to_convert}_{new_dtype}"
    if st.button("🔎 Preview Conversion", key=f"preview_dtype_{col_to_convert}"):
        st.session_state[prev_key] = _preview_conversion(df[col_to_convert], new_dtype)

    preview = st.session_state.get(prev_key)
    if preview:
        if "error" in preview:
            st.error(f"Preview failed: {preview['error']}")
        else:
            pct       = preview["pct"]
            new_nulls = preview["new_nulls"]
            total     = preview["total"]
            success   = preview["success"]

            if pct == 100:
                colour, icon = "#10b981", "✅"
            elif pct >= 80:
                colour, icon = "#f59e0b", "⚠️"
            else:
                colour, icon = "#ef4444", "❌"

            null_line = (
                f'<br><span style="color:#ef4444;font-size:0.82rem;">'
                f'⚠️ {new_nulls:,} value(s) will become <b>null</b> (unconvertible → NaN)</span>'
                if new_nulls else
                f'<br><span style="color:#10b981;font-size:0.82rem;">'
                f'No new null values will be introduced.</span>'
            )
            st.markdown(
                f'<div style="background:rgba(0,0,0,0.15);border-radius:12px;'
                f'padding:0.9rem 1.1rem;margin:0.5rem 0;">'
                f'<span style="font-size:1.05rem;font-weight:700;color:{colour};">'
                f'{icon} {pct}% success rate</span>'
                f'<span style="color:var(--text-muted);font-size:0.82rem;margin-left:0.8rem;">'
                f'— {success:,} of {total:,} values will convert cleanly</span>'
                f'{null_line}'
                f'<span style="color:var(--text-muted);font-size:0.78rem;margin-left:0.8rem;">'
                f' · Result dtype: <code>{preview["dtype_after"]}</code></span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.caption("Sample — before vs after (rows that fail show ⚠️ null):")
            st.dataframe(
                preview["sample_df"].style.apply(
                    lambda col: ["color:#ef4444" if "null" in str(v) else "" for v in col],
                    subset=["After"],
                ),
                use_container_width=True,
                hide_index=True,
            )

    if st.button("🔄 Apply Transformation", key=f"apply_dtype_{col_to_convert}"):
        with st.spinner(f"Converting `{col_to_convert}` to {new_dtype}…"):
            try:
                if new_dtype == "datetime64[ns]":
                    converted = pd.to_datetime(df[col_to_convert], errors='coerce')

                elif new_dtype == "date":
                    converted = pd.to_datetime(df[col_to_convert], errors='coerce').dt.date

                elif new_dtype == "time":
                    # Fully vectorised -- prepend dummy date so pd.to_datetime can
                    # parse pure time strings like "07:06:11", then store as "HH:MM:SS"
                    # strings (avoids Streamlit serialisation issues with time objects).
                    src = df[col_to_convert].astype(str).str.strip()
                    parsed = pd.to_datetime("1970-01-01 " + src, errors='coerce')
                    # Where dummy-date parse failed, try direct ISO parse
                    mask_failed = parsed.isna()
                    if mask_failed.any():
                        parsed[mask_failed] = pd.to_datetime(
                            src[mask_failed], errors='coerce')
                    converted = parsed.dt.strftime('%H:%M:%S').where(
                        parsed.notna(), other=None)

                elif new_dtype == "timedelta64[ns]":
                    src = df[col_to_convert]
                    # If column already holds datetime.time objects, stringify them
                    if len(src) > 0 and isinstance(src.iloc[0], datetime.time):
                        src = src.apply(
                            lambda v: f"{v.hour}:{v.minute:02d}:{v.second:02d}"
                            if isinstance(v, datetime.time) else str(v))
                    converted = pd.to_timedelta(src.astype(str), errors='coerce')

                elif new_dtype in ["string", "object"]:
                    converted = df[col_to_convert].astype(str)

                elif new_dtype == "category":
                    converted = df[col_to_convert].astype('category')

                elif new_dtype == "bool":
                    src = df[col_to_convert].astype(str).str.strip().str.lower()
                    converted = src.map({"true": True, "1": True, "yes": True,
                                         "false": False, "0": False, "no": False})
                    if converted.isna().all():
                        raise ValueError(
                            "No recognisable boolean values (expected true/false/1/0/yes/no).")

                elif new_dtype in ["int64", "float64"]:
                    converted = pd.to_numeric(
                        df[col_to_convert], errors='coerce').astype(new_dtype)

                else:
                    converted = df[col_to_convert].astype(new_dtype)

                n_null_before = int(df[col_to_convert].isna().sum())
                df[col_to_convert] = converted
                st.session_state.df = df
                # Clear preview cache now that the conversion is applied
                st.session_state.pop(prev_key, None)
                n_null_after = int(df[col_to_convert].isna().sum())
                new_nulls = max(0, n_null_after - n_null_before)
                msg = f"✅ Converted `{col_to_convert}` to `{new_dtype}`"
                if new_nulls:
                    msg += f" ({new_nulls} value(s) couldn't convert → became null)"
                st.success(msg)
                st.rerun()
            except Exception as e:
                st.error(f"❌ Conversion failed: {e}")

    # ── Geo Location Standardiser ─────────────────────────────────────────────
    # Detects columns that look like geographic location names and shows which
    # values failed to resolve to ISO codes, with a manual mapping editor so
    # users can fix them before running Map Plot.
    st.markdown("---")
    with st.expander("🌍 Geo Location Standardiser (for Map Plot)", expanded=False):
        st.markdown(
            "Use this tool when Map Plot shows the *'Could not resolve'* warning. "
            "Select a column containing country or state names, see which values "
            "couldn't be matched, and remap them to valid names."
        )
        try:
            from modules.analysis.map_plot import (
                _build_country_map, _build_us_state_map,
                resolve_geo_names, get_unresolved_values, _norm,
                _CHOROPLETH_SCALES,
            )
            # Include object, StringDtype, and category — all can hold location names.
            # Fall back to ALL columns so the user can manually pick any column.
            _str_cols = [
                c for c in df.columns
                if pd.api.types.is_string_dtype(df[c])
                or pd.api.types.is_object_dtype(df[c])
                or str(df[c].dtype) in ("string", "category")
            ]
            if not _str_cols:
                _str_cols = df.columns.tolist()   # last resort — show everything
            else:
                _geo_col = st.selectbox(
                    "Select location column", _str_cols,
                    key="geo_std_col",
                    help="The column you plan to use as the location in Map Plot.",
                )
                if _geo_col:
                    _series = df[_geo_col].dropna().astype(str)
                    _unique  = _series.unique()
                    _n_total = len(_unique)

                    # Run resolution and get unresolved list
                    _resolved, _geo_type = resolve_geo_names(
                        pd.Series(_unique), col_name=_geo_col
                    )
                    _unresolved = [
                        str(v) for v, r in zip(_unique, _resolved)
                        if r is None or (isinstance(r, float) and __import__("math").isnan(r))
                    ]
                    _n_ok      = _n_total - len(_unresolved)
                    _pct       = round(_n_ok / max(_n_total, 1) * 100)

                    # Status bar
                    if _geo_type == "unknown":
                        st.warning(
                            f"⚠️ Detected type: **unknown** — fewer than 40% of values matched. "
                            f"Try remapping values below, or check that the column contains "
                            f"standard country/state names."
                        )
                    else:
                        st.success(
                            f"✅ Detected: **{_geo_type}** — "
                            f"{_n_ok}/{_n_total} values resolved ({_pct}%)"
                        )

                    # Show unresolved values
                    if _unresolved:
                        st.markdown(
                            f"**{len(_unresolved)} unresolved value(s)** — "
                            f"enter the correct standard name next to each:"
                        )
                        _country_map = _build_country_map()
                        _state_map   = _build_us_state_map()
                        _all_known   = sorted(set(
                            list(_country_map.keys()) + list(_state_map.keys())
                        ))
                        # Load any saved remaps from session state
                        _remaps: dict = st.session_state.get("_geo_remaps", {}).get(_geo_col, {})

                        _new_remaps = {}
                        for _uv in _unresolved[:30]:  # cap at 30 to avoid massive UI
                            _r1, _r2 = st.columns([2, 3])
                            with _r1:
                                st.markdown(
                                    f'<div style="padding:6px 0;color:#f1f5f9;font-size:0.87rem;">'
                                    f'<code>{_uv}</code></div>',
                                    unsafe_allow_html=True,
                                )
                            with _r2:
                                _new_remaps[_uv] = st.text_input(
                                    "Remap to",
                                    value=_remaps.get(_uv, ""),
                                    placeholder="e.g. India  or  US  or  California",
                                    key=f"geo_remap_{_geo_col}_{_uv}",
                                    label_visibility="collapsed",
                                )
                        if len(_unresolved) > 30:
                            st.caption(f"… and {len(_unresolved) - 30} more. Fix the most common ones first.")

                        if st.button("✅ Apply Remaps to Dataset", key="geo_apply_remaps", type="primary"):
                            _filled = {k: v.strip() for k, v in _new_remaps.items() if v.strip()}
                            if not _filled:
                                st.warning("No remaps entered.")
                            else:
                                df = st.session_state.df.copy()
                                df[_geo_col] = df[_geo_col].astype(str).replace(_filled)
                                st.session_state.df = df
                                # Persist remaps so they survive reruns
                                _all_remaps = st.session_state.get("_geo_remaps", {})
                                _all_remaps[_geo_col] = _filled
                                st.session_state["_geo_remaps"] = _all_remaps
                                _n_changed = df[_geo_col].isin(_filled.values()).sum()
                                st.success(
                                    f"✅ Applied {len(_filled)} remap(s) — "
                                    f"{_n_changed:,} rows updated. "
                                    f"Re-open this panel to check remaining unresolved values."
                                )
                                st.rerun()
                    else:
                        st.success("🎉 All values in this column resolve cleanly — ready for Map Plot.")

                    # Preview resolved column
                    with st.expander("🔍 Preview resolved ISO codes", expanded=False):
                        _prev_resolved, _ = resolve_geo_names(df[_geo_col], col_name=_geo_col)
                        _prev_df = pd.DataFrame({
                            _geo_col: df[_geo_col].values,
                            "ISO Code": _prev_resolved.values,
                        }).drop_duplicates().head(20)
                        _prev_df["Status"] = _prev_df["ISO Code"].apply(
                            lambda x: "✅" if (x and isinstance(x, str) and len(x) >= 2) else "❌ unresolved"
                        )
                        st.dataframe(_prev_df, use_container_width=True, hide_index=True)
        except ImportError:
            st.info("pycountry not installed. Run: `pip install pycountry`")
        except Exception as _geo_err:
            st.error(f"Geo standardiser error: {_geo_err}")

    return df


def show_column_classifier(df):  # CRITICAL: sets num_cols/cat_cols/dt_cols used by ALL analysis runners.
    all_cols = df.columns.tolist()

    auto_dt = df.select_dtypes(include=['datetime','datetimetz','timedelta']).columns.tolist()
    for col in df.select_dtypes(include=['object']):
        if len(df) > 0 and isinstance(df[col].iloc[0], (datetime.date, datetime.time)):
            if col not in auto_dt:
                auto_dt.append(col)

    auto_num = df.select_dtypes(include=[np.number]).columns.tolist()
    auto_num = [c for c in auto_num if c not in auto_dt]
    auto_cat = [c for c in all_cols if c not in auto_num and c not in auto_dt]

    st.markdown("---")
    st.markdown("## 🏷️ Column Classification")
    # "Manual selection" radio was removed — auto-detect is always on.
    # The multiselect widgets below still allow the user to add/remove columns
    # from each bucket, so full manual control is preserved without the
    # confusing redundant mode toggle.
    st.markdown('<div class="classifier-box">', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1: confirmed_num = st.multiselect("Numeric Columns",     all_cols, default=auto_num, key="cls_num")
    with c2: confirmed_cat = st.multiselect("Categorical Columns", all_cols, default=auto_cat, key="cls_cat")
    with c3: confirmed_dt  = st.multiselect("Date/Time Columns",   all_cols, default=auto_dt,  key="cls_dt")
    st.markdown('</div>', unsafe_allow_html=True)

    overlap = []
    if set(confirmed_num) & set(confirmed_cat): overlap.append("Numeric & Categorical")
    if set(confirmed_num) & set(confirmed_dt):  overlap.append("Numeric & Date/Time")
    if set(confirmed_cat) & set(confirmed_dt):  overlap.append("Categorical & Date/Time")
    if overlap:
        st.warning(f"⚠️ Overlap detected between: {', '.join(overlap)}")

    # ── Save cleaned data as CSV ──────────────────────────────────────────────
    st.markdown("---")
    with st.expander("💾 Save Cleaned Data as CSV", expanded=False):
        st.caption(
            "Download the current (cleaned) dataset as a CSV file. "
            "The file reflects all cleaning operations applied so far."
        )
        import io as _io, datetime as _dt

        _csv_col1, _csv_col2 = st.columns([2, 1])
        with _csv_col1:
            _default_name = (
                st.session_state.get("file_name", "cleaned_data")
                .replace(".csv", "")
                .replace(".xlsx", "")
                .replace(".xls", "")
            )
            _csv_filename = st.text_input(
                "File name",
                value=f"{_default_name}_cleaned",
                placeholder="e.g. my_dataset_cleaned",
                key="csv_export_filename",
                help="The .csv extension will be added automatically.",
            )
        with _csv_col2:
            all_cols_for_idx = ["None (default integer index)"] + list(df.columns)
            _csv_index_choice = st.selectbox(
                "Index column (optional)",
                options=all_cols_for_idx,
                index=0,
                key="csv_export_index",
                help="Choose a column to use as the CSV row index, or keep the default integer index.",
            )

        _fname = (_csv_filename.strip() or "cleaned_data").rstrip(".csv") + ".csv"
        _use_col_as_idx = (
            _csv_index_choice
            if _csv_index_choice != "None (default integer index)"
            else None
        )

        _export_df = df.set_index(_use_col_as_idx) if _use_col_as_idx else df
        _csv_bytes = _export_df.to_csv(index=bool(_use_col_as_idx)).encode("utf-8")

        st.download_button(
            label=f"⬇️ Download",
            data=_csv_bytes,
            file_name=_fname,
            mime="text/csv",
            key="csv_export_download_btn",
            use_container_width=True,
        )
        st.caption(
            f"📊 {len(df):,} rows × {len(df.columns)} columns — "
            f"{len(_csv_bytes)/1024:.1f} KB"
        )

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("✅ Confirm & Proceed to Analysis", disabled=bool(overlap)):
            st.session_state.num_cols = confirmed_num
            st.session_state.cat_cols = confirmed_cat
            st.session_state.dt_cols  = confirmed_dt
            st.session_state.page     = "analysis"
            # Only reset charts when NOT in edit mode -- preserve saved charts
            if "editing_session_id" not in st.session_state:
                st.session_state.charts   = []
                st.session_state.selected_analyses = []
            st.rerun()
