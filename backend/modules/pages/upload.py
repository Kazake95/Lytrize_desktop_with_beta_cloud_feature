"""
modules/pages/upload.py -- File upload and column classification page.
"""

import streamlit as st
import pandas as pd
from html import escape

from modules.ui.column_manager import show_column_manager
from modules.ui.column_tools   import show_dtype_transformer, show_column_classifier
from modules.ui.data_cleaner   import show_data_cleaner
from modules.ui.excel_loader   import show_excel_loader
from modules.ui.css            import inject_footer, render_logo
from modules.utils.perf        import read_csv_fast, mem_mb
from modules.analysis.data_quality import run_data_quality
from modules.analysis.outlier import run_outlier_upload


def _is_excel(name: str) -> bool:
    return name.lower().endswith((".xlsx", ".xls"))


def _uploaded_signature(uploaded) -> str:
    file_id = getattr(uploaded, "file_id", None)
    size    = getattr(uploaded, "size", None)
    if file_id:
        return f"{uploaded.name}:{size}:{file_id}"
    return f"{uploaded.name}:{size}:{len(uploaded.getbuffer())}"


@st.cache_data(show_spinner=False)
def _read_csv_cached(file_bytes: bytes, filename: str) -> pd.DataFrame:
    import io
    return read_csv_fast(io.BytesIO(file_bytes))


def page_upload():
    render_logo()

    if st.button("← Home"):
        st.session_state.page = "home"
        st.rerun()

    st.markdown("## 📂 Upload Dataset")

    if st.session_state.get("is_guest", False):
        st.info(
            "⚠️ **Guest mode** — sessions are saved locally on this device. "
            "Sign in later to sync them to your cloud account.",
            icon=None,
        )
    if "editing_session_id" in st.session_state:
        fname = st.session_state.get("editing_file_name", "the original file")
        if st.session_state.pop("_edit_needs_reupload", False):
            st.warning(
                f"✏️ **Editing session: \"{st.session_state.get('editing_session_name', '')}\"**\n\n"
                f"The original dataset (**{fname}**) needs to be re-uploaded to add or "
                f"modify charts. Your existing charts and settings are preserved — "
                f"just upload the same file type and you'll be taken straight to the analysis.",
                icon="⚠️",
            )
        else:
            st.info(
                f"✏️ **Edit mode** — re-upload **{fname}** to add more charts to the saved session."
            )

    uploaded = st.file_uploader(
        "CSV or Excel (single or multi-sheet) — up to 500 MB",
        type=["csv", "xlsx", "xls"],
    )

    # ── Resume existing session (navigated back from Analysis) ───────────────
    # When the user clicks "Upload" from the Analysis page, the file_uploader
    # widget starts empty (Streamlit doesn't persist uploaded files across page
    # navigations). If a df is already loaded we show the existing pipeline
    # rather than leaving the user with a blank upload screen.
    if not uploaded and "df" in st.session_state and st.session_state.get("file_name"):
        _resumed_name = st.session_state["file_name"]
        st.info(
            f"📂 **{_resumed_name}** is still loaded from your last session. "
            "You can continue cleaning and transforming, or upload a new file above to replace it.",
            icon=None,
        )
        _col_resume, _col_clear = st.columns([2, 1])
        with _col_resume:
            if st.button("▶ Continue with current dataset", key="_resume_dataset",
                         type="primary", use_container_width=True):
                st.session_state["_resume_upload"] = True
                st.rerun()
        with _col_clear:
            if st.button("🗑 Start fresh (clear dataset)", key="_clear_dataset",
                         use_container_width=True):
                for k in ["df", "file_name", "file_signature", "_dq_charts", "_dq_sig",
                          "_ul_preview_mode", "_resume_upload"]:
                    st.session_state.pop(k, None)
                st.rerun()

        # Render the pipeline if the user already confirmed to resume
        if st.session_state.get("_resume_upload"):
            _show_analysis_pipeline(st.session_state["df"], _resumed_name)
        inject_footer()
        return

    if not uploaded:
        inject_footer()
        return

    is_excel     = _is_excel(uploaded.name)
    file_sig     = _uploaded_signature(uploaded)
    file_changed = (
        st.session_state.get("file_name")      != uploaded.name or
        st.session_state.get("file_signature") != file_sig
    )
    # Clear the resume flag now that a real file is in the widget
    st.session_state.pop("_resume_upload", None)

    if not is_excel:
        if "df" not in st.session_state or file_changed:
            with st.spinner("Reading and optimising file…"):
                uploaded.seek(0)
                file_bytes = uploaded.read()
                df = _read_csv_cached(file_bytes, uploaded.name)
            st.session_state.df             = df
            st.session_state.file_name      = uploaded.name
            st.session_state.file_signature = file_sig
            _clear_excel_state()
            mb = mem_mb(df)
            if mb > 50:
                st.caption(f"📊 Loaded {df.shape[0]:,} rows — memory footprint: {mb:.0f} MB")
        else:
            df = st.session_state.df
        _show_analysis_pipeline(df, uploaded.name)
    else:
        # Excel path (unchanged)
        if file_changed:
            st.session_state.pop("df", None)
            _clear_excel_state(uploaded.name)
            st.session_state.file_name      = uploaded.name
            st.session_state.file_signature = file_sig

        if "df" not in st.session_state:
            df = show_excel_loader(uploaded)
            if df is not None:
                st.session_state.df = df
                st.rerun()
        else:
            if st.button("⚙️ Edit Excel Configuration", key="_xl_edit_config"):
                st.session_state.pop("df", None)
                st.rerun()
            _show_analysis_pipeline(st.session_state.df, uploaded.name)


def _show_analysis_pipeline(df: pd.DataFrame, file_name: str):
    st.markdown("---")
    _n_rows = df.shape[0]
    _n_cols = df.shape[1]
    st.success(f"✅ **{file_name}** — {_n_rows:,} rows × {_n_cols} columns")

    # ── Interactive data preview ──────────────────────────────────────────────
    st.markdown("### 📋 Data Preview")
    _pb1, _pb2, _pb3, _pb4 = st.columns([1, 1, 1, 4])
    with _pb1:
        if st.button("⬆ Top 10",    key="ul_prev_top",  use_container_width=True):
            st.session_state["_ul_preview_mode"] = "top"
    with _pb2:
        if st.button("⬇ Bottom 10", key="ul_prev_bot",  use_container_width=True):
            st.session_state["_ul_preview_mode"] = "bottom"
    with _pb3:
        if st.button("🎲 Random",   key="ul_prev_rand", use_container_width=True):
            st.session_state["_ul_preview_mode"] = "random"
    with _pb4:
        _num_c = len(df.select_dtypes("number").columns)
        _cat_c = len(df.select_dtypes("object").columns)
        _dt_c  = len(df.select_dtypes("datetime").columns)
        _null  = round(df.isnull().sum().sum() / max(df.size, 1) * 100, 1)
        st.caption(
            f"🔢 {_num_c} numeric  ·  🔤 {_cat_c} text  ·  "
            f"📅 {_dt_c} datetime  ·  ⚠️ {_null}% missing"
        )

    _ul_mode = st.session_state.get("_ul_preview_mode", "top")
    try:
        if _ul_mode == "bottom":
            _prev_df = df.tail(10)
            _lbl = "Bottom 10 rows"
        elif _ul_mode == "random":
            _prev_df = df.sample(min(10, _n_rows), random_state=None)
            _lbl = "10 random rows"
        else:
            _prev_df = df.head(10)
            _lbl = "Top 10 rows"
    except Exception:
        _prev_df = df.head(10)
        _lbl = "Top 10 rows"

    st.caption(f"*{_lbl}*")
    st.dataframe(_prev_df, use_container_width=True, height=min(380, 38 + len(_prev_df) * 35))

    st.markdown("### 🧹 Data Quality")
    # Cache data quality charts keyed to df shape+columns so we don't re-run the
    # expensive quality scan on every widget interaction rerun of the upload page.
    _dq_sig = f"dq_{df.shape}_{list(df.columns)}"
    if st.session_state.get("_dq_sig") != _dq_sig:
        with st.spinner("Running data quality checks…"):
            dq_charts = run_data_quality(df)
        st.session_state["_dq_charts"] = dq_charts
        st.session_state["_dq_sig"]    = _dq_sig
    else:
        dq_charts = st.session_state.get("_dq_charts", [])
    if dq_charts:
        st.markdown("#### Data Quality Summaries")
        for title, fig in dq_charts:
            st.plotly_chart(fig, use_container_width=True)
    st.markdown("---")

    st.markdown("### 🔍 Outlier Detection")
    run_outlier_upload(df)
    st.markdown("---")

    df = show_column_manager(df)
    df = show_dtype_transformer(df)
    df = show_data_cleaner(df)
    show_column_classifier(df)

    with st.expander("📖 Describe Your Columns (optional)", expanded=False):
        st.markdown("Describe what each column means for better auto-insights.")
        col_descs = st.session_state.get("col_descriptions", {})
        for col in df.columns:
            col_descs[col] = st.text_input(
                f"`{col}`",
                value=col_descs.get(col, ""),
                key=f"coldesc_{col}",
                placeholder="e.g. 'Total revenue in USD'",
            )
        if st.button("💾 Save Column Descriptions", key="save_col_descs"):
            st.session_state.col_descriptions = col_descs
            st.success("✅ Saved.")


def _clear_excel_state(new_file_name: str = "") -> None:
    keys_to_delete = [
        k for k in list(st.session_state.keys())
        if k.startswith("_xl_sheets_") and (
            not new_file_name or not k.endswith(new_file_name)
        )
    ]
    for k in keys_to_delete:
        del st.session_state[k]
    st.session_state.pop("_unified_table_info", None)