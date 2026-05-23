"""
modules/analysis/data_quality.py
=================================

Performance design:
  - @st.cache_data on all heavy pandas/Plotly computations — only recompute
    when df actually changes, not on every widget interaction.
  - @st.fragment on interactive sections — selectbox changes and button
    clicks only rerun the fragment, not the whole page. No view shift.
  - Direct action execution in button handlers (no deferred-pending pattern)
    = 2 renders per action instead of the previous 3.
  - st.toast() for all feedback — floating overlay, zero layout shift.
"""

import streamlit as st
import plotly.express as px
import pandas as pd
from modules.database import log_activity
from modules.charts import chart_layout, DANGER


# ── Cached heavy computations ─────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _missing_summary(df: pd.DataFrame) -> pd.DataFrame:
    mc = df.isnull().sum().reset_index()
    mc.columns = ["Column", "Missing Count"]
    mc["Missing %"] = (mc["Missing Count"] / len(df) * 100).round(2)
    mc = mc[mc["Missing Count"] > 0].sort_values("Missing Count", ascending=False)
    return mc.reset_index(drop=True)


@st.cache_data(show_spinner=False)
def _missing_row_mask(df: pd.DataFrame) -> pd.Series:
    return df.isnull().any(axis=1)


@st.cache_data(show_spinner=False)
def _dup_count(df: pd.DataFrame, pk_col) -> int:
    subset = [pk_col] if pk_col else None
    return int(df.duplicated(subset=subset).sum())


@st.cache_data(show_spinner=False)
def _dup_rows(df: pd.DataFrame, pk_col) -> pd.DataFrame:
    subset = [pk_col] if pk_col else None
    mask = df.duplicated(subset=subset, keep=False)
    return df[mask].sort_values(by=df.columns.tolist())


@st.cache_data(show_spinner=False)
def _chart_missing_bar(mc: pd.DataFrame):
    fig = px.bar(
        mc, x="Column", y="Missing %",
        title="Missing % by Column",
        color="Missing %",
        color_continuous_scale=DANGER,
        text_auto=".1f",
    )
    fig.update_layout(**chart_layout())
    return fig


@st.cache_data(show_spinner=False)
def _chart_missing_heatmap(df: pd.DataFrame):
    sample = df.sample(min(100, len(df)), random_state=42) if len(df) > 100 else df
    fig = px.imshow(
        sample.isnull().astype(int),
        title="Missing Values Map",
        color_continuous_scale=["rgba(0,0,0,0)", "#ef4444"],
        aspect="auto",
    )
    fig.update_layout(**chart_layout())
    return fig


@st.cache_data(show_spinner=False)
def _chart_dup_donut(total: int, dup_count: int):
    fig = px.pie(
        values=[total - dup_count, dup_count],
        names=["Unique", "Duplicate"],
        title=f"Row Uniqueness — {dup_count:,} duplicates of {total:,}",
        color_discrete_sequence=["#6163df", "#ef4444"],
        hole=0.48,
    )
    fig.update_layout(**chart_layout())
    return fig


# ── Fragment: missing-value interactive controls ───────────────────────────────
# st.fragment isolates this block so selectbox / button interactions only
# rerun this section — the rest of the page stays frozen.

@st.fragment
def _missing_controls(df: pd.DataFrame):
    """Interactive cleaning controls for missing values — runs as isolated fragment."""
    uid = st.session_state.get("user_id", 0)
    mc  = _missing_summary(df)

    st.dataframe(mc, use_container_width=True, hide_index=True)

    row_mask    = _missing_row_mask(df)
    n_miss_rows = int(row_mask.sum())
    with st.expander(f"👁️ View rows with missing values ({n_miss_rows:,} rows)"):
        st.dataframe(df[row_mask].head(200), use_container_width=True)

    st.markdown("**🧹 Clean missing values:**")
    cl1, cl2, cl3 = st.columns([1, 2, 2])

    with cl1:
        if st.button("Drop rows with ANY NA", key="dq_dropna_all"):
            before = len(st.session_state.df)
            st.session_state.df = st.session_state.df.dropna().reset_index(drop=True)
            removed = before - len(st.session_state.df)
            log_activity(uid, "dropna_all", f"removed {removed} rows")
            st.toast(f"✅ Removed {removed:,} rows — {len(st.session_state.df):,} remain", icon="✅")
            st.rerun()

    with cl2:
        col_to_drop = st.selectbox(
            "Column:",
            mc["Column"].tolist(),
            key="dq_col_na",
            label_visibility="collapsed",
        )

    with cl3:
        if st.button(f"Drop NA in '{col_to_drop}'", key="dq_dropna_col"):
            before = len(st.session_state.df)
            st.session_state.df = st.session_state.df.dropna(
                subset=[col_to_drop]).reset_index(drop=True)
            removed = before - len(st.session_state.df)
            log_activity(uid, "dropna_col", f"col={col_to_drop} removed={removed}")
            st.toast(f"✅ Removed {removed:,} rows where '{col_to_drop}' was NA", icon="✅")
            st.rerun()


# ── Fragment: duplicate-row interactive controls ───────────────────────────────

@st.fragment
def _dup_controls(df: pd.DataFrame):
    """Interactive duplicate detection and deletion — runs as isolated fragment."""
    uid = st.session_state.get("user_id", 0)

    with st.expander("ℹ️ What is a Primary Key?", expanded=False):
        st.markdown(
            "A **primary key** uniquely identifies each row — like *Order ID* or "
            "*Customer ID*. Selecting one detects duplicates by that column alone, "
            "which is far more accurate than comparing every field. "
            "Leave as *None* to compare all columns."
        )

    pk_options = ["None (compare all columns)"] + df.columns.tolist()
    pk_choice  = st.selectbox(
        "Primary key column (optional):",
        options=pk_options,
        key="dq_pk_col",
    )
    pk_col = None if pk_choice == "None (compare all columns)" else pk_choice

    n_dups   = _dup_count(df, pk_col)
    scope    = f"**{pk_choice}**" if pk_col else "**all columns**"
    st.caption(f"Scope: {scope} — {n_dups:,} duplicate(s) found.")

    if n_dups == 0:
        st.success("✅ No duplicate rows found!")
        return

    st.markdown(f"**{n_dups:,} duplicate rows** detected.")
    dup_df = _dup_rows(df, pk_col)

    with st.expander(f"👁️ View duplicates ({len(dup_df):,} rows incl. originals)"):
        st.dataframe(dup_df.head(500), use_container_width=True)
        st.markdown("**Delete individual rows** (by row index):")
        del_idx = st.multiselect(
            "Select indices:",
            options=dup_df.index.tolist(),
            key="dq_del_idx",
            help="Row indices in the original DataFrame",
        )
        if st.button(
            f"🗑️ Delete {len(del_idx) or ''} selected row(s)",
            key="dq_del_selected",
            disabled=not del_idx,
        ):
            st.session_state.df = (
                st.session_state.df.drop(index=del_idx).reset_index(drop=True)
            )
            log_activity(uid, "delete_rows_manual", f"deleted {len(del_idx)} rows")
            st.toast(f"✅ Deleted {len(del_idx)} selected row(s)", icon="✅")
            st.rerun()

    d1, d2 = st.columns(2)
    with d1:
        if st.button("Drop ALL duplicates (keep first)", key="dq_drop_dup"):
            subset = [pk_col] if pk_col else None
            before = len(st.session_state.df)
            st.session_state.df = (
                st.session_state.df
                .drop_duplicates(subset=subset, keep="first")
                .reset_index(drop=True)
            )
            removed = before - len(st.session_state.df)
            log_activity(uid, "drop_duplicates", f"removed {removed} keep=first pk={pk_col}")
            st.toast(f"✅ Removed {removed:,} duplicates (kept first)", icon="✅")
            st.rerun()

    with d2:
        if st.button("Drop ALL duplicates (keep last)", key="dq_drop_dup_last"):
            subset = [pk_col] if pk_col else None
            before = len(st.session_state.df)
            st.session_state.df = (
                st.session_state.df
                .drop_duplicates(subset=subset, keep="last")
                .reset_index(drop=True)
            )
            removed = before - len(st.session_state.df)
            log_activity(uid, "drop_duplicates_last", f"removed {removed} keep=last pk={pk_col}")
            st.toast(f"✅ Removed {removed:,} duplicates (kept last)", icon="✅")
            st.rerun()


# ── Public entry point ────────────────────────────────────────────────────────

def run_data_quality(df: pd.DataFrame) -> list:
    """
    Render interactive data quality widgets and return summary charts.

    Renders two @st.fragment sections (missing values + duplicates) so that
    widget interactions only rerun their fragment, not the whole page.

    Returns:
        list of (title, fig) tuples for upload.py to display inline.
    """
    charts = []

    # ── Section 1: Missing Values ─────────────────────────────────────────────
    miss_total = int(df.isnull().sum().sum())
    st.markdown("### 🕳️ Missing Values")

    if miss_total == 0:
        st.success("✅ No missing values — dataset is complete!")
    else:
        mc = _missing_summary(df)
        n_miss_rows = int(_missing_row_mask(df).sum())
        st.markdown(
            f"**{miss_total:,} missing cells** across "
            f"{len(mc)} column(s) in {n_miss_rows:,} rows"
        )
        _missing_controls(df)                          # ← isolated fragment
        charts.append(("Missing % by Column",  _chart_missing_bar(mc)))
        charts.append(("Missing Values Map",   _chart_missing_heatmap(df)))

    st.markdown("---")

    # ── Section 2: Duplicate Rows ─────────────────────────────────────────────
    st.markdown("### 🔁 Duplicate Rows")
    _dup_controls(df)                                  # ← isolated fragment

    # Donut always shown (cached, no heavy computation)
    n_dups = _dup_count(df, None)
    charts.append(("Duplicate Rows Summary", _chart_dup_donut(len(df), n_dups)))

    return charts
