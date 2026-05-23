"""
modules/ui/excel_loader.py -- Excel multi-sheet browser and unified table builder.
==================================================================================

Handles Excel files (.xlsx / .xls) uploaded on the upload page.

Two modes:

  Sheet browser mode (default):
      Renders a tab per sheet. The user picks the sheet they want to analyse,
      which is loaded into st.session_state.df.

  Unified table mode (optional):
      Lets the user select multiple sheets and stack them vertically into one
      DataFrame. Useful for "monthly sheets" style workbooks where each sheet
      has the same columns but different rows.

Session state keys managed here (all prefixed _xl_):
    _xl_sheets_{filename}    -- dict of {sheet_name: DataFrame}
    _unified_table_info      -- metadata about the current unified table

These keys are cleared by _clear_excel_state() in upload.py whenever the
active file changes, so stale sheet data from a previous upload never bleeds
into a new one.

CONTRIBUTING -- to support a new Excel feature (e.g. named ranges):
    Add a new expander inside show_excel_loader() after the unified table section.
    Read data from the openpyxl workbook (available via pd.ExcelFile) and
    write the result into st.session_state.df using the same pattern as
    the existing sheet / unified table loaders.
"""
"""
modules/ui/excel_loader.py

Handles Excel files with multiple sheets. Gives the user two paths:

PATH A -- Single Sheet
    Browse all sheets with previews, pick one, proceed as normal.

PATH B -- Unified Table builder
    Pick a Fact table sheet + Dimension table sheets, map join keys
    between each Dim and the Fact, choose join type, then merge everything
    into one flat DataFrame that feeds into the standard analysis pipeline.

** Base table = Fact table in variables & cod ; Additional table = Dim table in variables & code

Returns a ready-to-use pd.DataFrame (or None if the user has not confirmed yet).
"""

import streamlit as st
import pandas as pd
import numpy as np
from html import escape
from itertools import combinations


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_all_sheets(file) -> dict[str, pd.DataFrame]:  # Read every sheet into a dict {sheet_name: DataFrame}.
    """Read every sheet from the uploaded Excel file into a dict."""
    file.seek(0)
    return pd.read_excel(file, sheet_name=None)   # sheet_name=None → all sheets


def _file_key(uploaded_file) -> str:
    file_id = getattr(uploaded_file, "file_id", None)
    size = getattr(uploaded_file, "size", None)
    token = file_id or f"{size}_{len(uploaded_file.getbuffer())}"
    return f"_xl_sheets_{uploaded_file.name}_{token}"


def _common_columns(df_a: pd.DataFrame, df_b: pd.DataFrame) -> list[str]:
    """Return column names present in both DataFrames (case-sensitive)."""
    return sorted(set(df_a.columns) & set(df_b.columns))


def _shape_tag(df: pd.DataFrame) -> str:
    return f"{df.shape[0]:,} rows × {df.shape[1]} cols"


def _dtype_summary(df: pd.DataFrame) -> str:
    n = df.select_dtypes(include=np.number).shape[1]
    c = df.select_dtypes(include="object").shape[1]
    d = df.select_dtypes(include=["datetime", "datetimetz"]).shape[1]
    parts = []
    if n: parts.append(f"{n} numeric")
    if c: parts.append(f"{c} text")
    if d: parts.append(f"{d} date")
    return " · ".join(parts) if parts else "no cols"


# ── Main entry point ──────────────────────────────────────────────────────────

def show_excel_loader(uploaded_file) -> pd.DataFrame | None:  # Two paths: single-sheet picker OR star-schema multi-sheet join.
    """
    Renders the sheet-picker UI for Excel files.
    Returns a merged/selected DataFrame when the user confirms,
    or None while they are still configuring.
    """
    file_key = _file_key(uploaded_file)

    # Cache sheets in session state so we don't re-read on every widget interaction
    if file_key not in st.session_state:
        with st.spinner("Reading all sheets…"):
            st.session_state[file_key] = _load_all_sheets(uploaded_file)

    sheets: dict[str, pd.DataFrame] = st.session_state[file_key]
    sheet_names = list(sheets.keys())

    if len(sheet_names) == 1:
        # Only one sheet -- just load it, nothing to choose
        st.info(f"📋 Single sheet detected: **{sheet_names[0]}**")
        return sheets[sheet_names[0]]

    # ── Sheet overview cards ──────────────────────────────────────────────────
    st.markdown("### 📑 Sheets in this workbook")
    cols = st.columns(min(len(sheet_names), 4))
    for i, name in enumerate(sheet_names):
        df_s = sheets[name]
        safe_name = escape(str(name))
        with cols[i % 4]:
            st.markdown(
                f"""<div style="background:rgba(97,99,223,0.08);border:1px solid rgba(97,99,223,0.2);
                border-radius:12px;padding:0.9rem 1rem;margin-bottom:0.5rem;">
                <div style="font-weight:700;font-size:0.9rem;margin-bottom:4px;">📄 {safe_name}</div>
                <div style="font-size:0.75rem;opacity:0.75;">{_shape_tag(df_s)}</div>
                <div style="font-size:0.72rem;opacity:0.6;">{_dtype_summary(df_s)}</div>
                </div>""",
                unsafe_allow_html=True
            )

    st.markdown("---")

    # ── Mode selector ─────────────────────────────────────────────────────────
    mode = st.radio(
        "How do you want to use this file?",
        options=[
            "📋  Use a single sheet for analysis",
            "🔗  Model multiple sheets (Table Join)",
        ],
        key="_xl_mode",
        horizontal=False,
    )

    # ═════════════════════════════════════════════════════════════════════════
    # PATH A -- Single sheet picker
    # ═════════════════════════════════════════════════════════════════════════
    if "single sheet" in mode:
        st.markdown("#### Select a sheet")
        selected = st.selectbox(
            "Sheet to analyse:",
            sheet_names,
            key="_xl_single_sheet",
        )
        df_preview = sheets[selected]

        with st.expander(f"👁️ Preview -- {selected}  ({_shape_tag(df_preview)})", expanded=True):
            st.dataframe(df_preview.head(10), use_container_width=True)

        if st.button("✅ Use this sheet →", key="_xl_confirm_single"):
            st.success(f"✅ Loaded sheet **{selected}** ({_shape_tag(df_preview)})")
            return df_preview

        return None   # waiting for user to confirm

    # ═════════════════════════════════════════════════════════════════════════
    # PATH B -- Multi-table join
    # ═════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("### 🔗 Unified Table Builder")
    st.markdown(
        "Merge all your sheets into one comprehensive table. Perfect for analyzing related data across multiple sheets without complex joins. "
        "Step 1 -- Select your Base table "
        "This table will be the foundation. We'll attach other sheets to it automatically."
    )

    # Step 1 -- Pick fact table
    st.markdown("#### Step 1 -- Choose your Fact table")
    st.caption("TThe Base table is your main dataset with unique metrics like (transactions, records, events etc). Other sheets will be merged into this table. It's usually the largest or most central sheet.")
    fact_name = st.selectbox("Fact table sheet:", sheet_names, key="_xl_fact")
    fact_df   = sheets[fact_name]

    with st.expander(f"👁️ Base table preview -- {fact_name}  ({_shape_tag(fact_df)})", expanded=False):
        st.dataframe(fact_df.head(8), use_container_width=True)

    # Step 2 -- Pick dimension tables
    st.markdown("#### Step 2 -- Choose Dimension tables to join")
    st.caption("Pick sheets to combine with your Primary table. We'll automatically link them together using matching columns to create one complete dataset.")
    dim_options = [s for s in sheet_names if s != fact_name]
    selected_dims = st.multiselect(
        "Dimension table sheets:",
        dim_options,
        key="_xl_dims",
    )

    if not selected_dims:
        st.info("Select at least one Additional table to continue.")
        return None

    # Step 3 -- Configure join for each dim
    st.markdown("#### Step 3 -- Map join keys")
    st.caption(
        "For each additional table, select the column that matches your Primary table. "
        "We'll use these shared columns to correctly merge your datasets."
    )

    join_configs: list[dict] = []
    all_valid = True

    for dim_name in selected_dims:
        dim_df = sheets[dim_name]
        common = _common_columns(fact_df, dim_df)

        st.markdown(f"**🔗 {fact_name}  ←→  {dim_name}**")

        if not common:
            st.info(
                f"💡 No columns with the exact same name were found between **{fact_name}** and **{dim_name}**. "
                f"Please manually select the matching keys below."
            )

        # 2. Safely determine default indices (fallback to 0 if no common columns)
        fact_idx = fact_df.columns.tolist().index(common[0]) if common and common[0] in fact_df.columns else 0
        dim_idx = dim_df.columns.tolist().index(common[0]) if common and common[0] in dim_df.columns else 0

        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            fact_key = st.selectbox(
                f"Fact key ({fact_name})",
                options=fact_df.columns.tolist(),
                index=fact_idx,
                key=f"_xl_fact_key_{dim_name}",
                help="The foreign key column in your Fact table"
            )
        with c2:
            dim_key = st.selectbox(
                f"Dim key ({dim_name})",
                options=dim_df.columns.tolist(),
                index=dim_idx,
                key=f"_xl_dim_key_{dim_name}",
                help="The primary key column in the Dimension table"
            )
        with c3:
            join_type = st.selectbox(
                "Join type",
                options=["left", "inner"],
                key=f"_xl_join_type_{dim_name}",
                help="Left: keep all fact rows. Inner: only matched rows."
            )

        # Show key match quality
        fact_vals = set(fact_df[fact_key].dropna().unique())
        dim_vals  = set(dim_df[dim_key].dropna().unique())
        match_pct = len(fact_vals & dim_vals) / len(fact_vals) * 100 if fact_vals else 0
        unmatched = len(fact_vals - dim_vals)

        if match_pct == 100:
            st.success(f"✅ 100% key match -- all Fact rows will join successfully.")
        elif match_pct >= 70:
            st.warning(f"⚠️ {match_pct:.0f}% key match -- {unmatched:,} Fact rows have no matching Dim record (will be null on left join).")
        else:
            st.error(f"❌ Only {match_pct:.0f}% key match -- check that you selected the right columns.")

        # Columns to bring from dim (exclude the join key to avoid duplication)
        with st.expander(f"📋 Choose columns to import from {dim_name}", expanded=False):
            all_dim_cols = [c for c in dim_df.columns if c != dim_key]
            selected_dim_cols = st.multiselect(
                f"Columns from {dim_name}:",
                options=all_dim_cols,
                default=all_dim_cols,
                key=f"_xl_dim_cols_{dim_name}",
                help="Uncheck columns you do not want in the final table"
            )

        join_configs.append({
            "dim_name":      dim_name,
            "dim_df":        dim_df,
            "fact_key":      fact_key,
            "dim_key":       dim_key,
            "join_type":     join_type,
            "cols_to_bring": selected_dim_cols,
        })

    if not all_valid:
        return None

    # Step 4 -- Schema diagram (text representation)
    st.markdown("---")
    st.markdown("#### Step 4 -- Unifier summary")

    schema_lines = [f"**FACT:** 📊 {fact_name}  ({_shape_tag(fact_df)})"]
    for cfg in join_configs:
        arrow = "←→" if cfg["join_type"] == "inner" else "←"
        schema_lines.append(
            f"  {arrow}  **DIM:** 📋 {cfg['dim_name']}  "
            f"(join on `{cfg['fact_key']}` = `{cfg['dim_key']}`,  "
            f"{cfg['join_type']} join,  "
            f"{len(cfg['cols_to_bring'])} cols imported)"
        )

    for line in schema_lines:
        st.markdown(line)

    # Estimate output shape
    est_rows = len(fact_df)
    est_cols = len(fact_df.columns) + sum(len(c["cols_to_bring"]) for c in join_configs)
    st.markdown(f"**Estimated output:** ~{est_rows:,} rows × {est_cols} columns")

    # ── Build button ──────────────────────────────────────────────────────────
    st.markdown("")
    if st.button("🔗 Build Merged Table & Proceed to Analysis →", key="_xl_confirm_schema"):
        with st.spinner("Merging tables…"):
            merged = fact_df.copy()
            merge_log = []

            for cfg in join_configs:
                dim_subset = cfg["dim_df"][[cfg["dim_key"]] + cfg["cols_to_bring"]].copy()

                # Suffix duplicate column names to avoid clashes
                overlap = [c for c in cfg["cols_to_bring"] if c in merged.columns]
                if overlap:
                    rename_map = {c: f"{c}_{cfg['dim_name']}" for c in overlap}
                    dim_subset = dim_subset.rename(columns=rename_map)

                before = len(merged)
                merged = merged.merge(
                    dim_subset,
                    left_on=cfg["fact_key"],
                    right_on=cfg["dim_key"],
                    how=cfg["join_type"],
                    suffixes=("", f"_{cfg['dim_name']}")
                )

                # Drop duplicate key column that merge adds
                if cfg["dim_key"] in merged.columns and cfg["dim_key"] != cfg["fact_key"]:
                    merged = merged.drop(columns=[cfg["dim_key"]], errors="ignore")

                after = len(merged)
                merge_log.append(
                    f"✅ Joined **{cfg['dim_name']}** ({cfg['join_type']}) -- "
                    f"{before:,} → {after:,} rows, +{len(cfg['cols_to_bring'])} columns"
                )

        st.markdown("**Merge log:**")
        for entry in merge_log:
            st.markdown(f"- {entry}")

        st.success(
            f"✅ Unified Table built -- **{merged.shape[0]:,} rows × {merged.shape[1]} columns** "
            f"ready for analysis."
        )
        #st.dataframe(merged.head(10), use_container_width=True)

        # Unified Table metadata for display later
        st.session_state["_unified_table_info"] = {
            "fact": fact_name,
            "dims": [c["dim_name"] for c in join_configs],
            "shape": merged.shape,
        }

        return merged

    return None   # waiting for user confirmation
