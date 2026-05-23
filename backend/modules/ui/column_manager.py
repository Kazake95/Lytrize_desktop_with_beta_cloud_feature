"""
modules/ui/column_manager.py -- Dashboard column-layout helpers.
================================================================

Provides layout utility functions used by the dashboard page to arrange
chart cards in portrait (2-column) or landscape (3-column) grids.

Functions:
    get_column_layout(layout_mode)  -- returns the Streamlit column spec list
    render_chart_grid(charts, ...)  -- renders all chart cards into the grid

CONTRIBUTING -- to add a new layout mode:
    1. Add an entry to the layout_mode selectbox in dashboard.py.
    2. Add a matching branch in get_column_layout() returning a column spec.
       Streamlit column specs are lists of relative widths, e.g. [1, 1, 1].
"""
"""
modules/ui/column_manager.py
Column add / remove UI shown on the upload page.
"""

import streamlit as st
import numpy as np
import pandas as pd
import ast
import operator
import re
from modules.database import log_activity


_SAFE_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_SAFE_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_formula_eval(df: pd.DataFrame, formula: str) -> pd.Series:
    """
    Evaluate a derived-column formula with arithmetic only.

    Supported:
      - numeric columns by plain name, e.g. Sales / Units
      - any column in backticks, e.g. `Total Sales` / `Units Sold`
      - constants, parentheses, +, -, *, /, //, %, **
    """
    numeric_cols = set(df.select_dtypes(include=[np.number]).columns)
    column_aliases: dict[str, str] = {}

    def replace_backtick(match: re.Match) -> str:
        col = match.group(1)
        if col not in df.columns:
            raise ValueError(f"Unknown column: {col}")
        alias = f"__col_{len(column_aliases)}"
        column_aliases[alias] = col
        return alias

    expr = re.sub(r"`([^`]+)`", replace_backtick, formula.strip())
    if not expr:
        raise ValueError("Formula is empty.")

    tree = ast.parse(expr, mode="eval")

    def eval_node(node):
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            col = column_aliases.get(node.id, node.id)
            if col not in df.columns:
                raise ValueError(f"Unknown column: {col}")
            if col not in numeric_cols:
                raise ValueError(f"Column '{col}' is not numeric.")
            return df[col]
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BIN_OPS:
            return _SAFE_BIN_OPS[type(node.op)](eval_node(node.left), eval_node(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_UNARY_OPS:
            return _SAFE_UNARY_OPS[type(node.op)](eval_node(node.operand))
        raise ValueError("Only arithmetic with numeric columns and constants is allowed.")

    result = eval_node(tree)
    if np.isscalar(result):
        return pd.Series([result] * len(df), index=df.index)
    return result


def show_column_manager(df):  # Add Column / Remove Column UI shown on the upload page.
    st.markdown("---")
    st.markdown("## 🛠️ Column Manager")
    tab_add, tab_remove = st.tabs(["➕ Add Column", "🗑️ Remove Column"])  # Two tabs: one to add derived columns, one to remove existing ones.

    with tab_add:
        num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        a1, a2 = st.columns(2)
        with a1: new_col_name = st.text_input("New column name", key="new_col_name")
        with a2:
            calc_type = st.selectbox("Calculation type", [
                "Custom formula (use col names)", "Column × Column", "Column ÷ Column",
                "Column + Column", "Column − Column", "Extract Date/Time Part"
            ], key="calc_type")

        formula_str = date_col = part_to_extract = None

        if calc_type == "Custom formula (use col names)":  # User types a pandas eval() expression using column names.
            formula_str = st.text_input("Formula", key="custom_formula", placeholder="e.g. Sales / Units")
        elif calc_type in ("Column × Column", "Column ÷ Column", "Column + Column", "Column − Column"):
            op_map = {"Column × Column":"*","Column ÷ Column":"/","Column + Column":"+","Column − Column":"-"}
            op = op_map[calc_type]
            b1, b2 = st.columns(2)
            with b1: col_a = st.selectbox("First", num_cols, key="col_a")
            with b2: col_b = st.selectbox("Second", num_cols, key="col_b")
            formula_str = f"`{col_a}` {op} `{col_b}`"
        elif calc_type == "Extract Date/Time Part":
            b1, b2 = st.columns(2)
            with b1: date_col = st.selectbox("Source Date Column", df.columns, key="date_col")
            with b2: part_to_extract = st.selectbox("Part to Extract",
                ["Year","Quarter","Month (Number)","Month Name","Week Number",
                 "Day","Weekday Name","Hour (12h AM/PM)","Hour (24h)"], key="date_part_ext")
            formula_str = "date_extraction_placeholder"

        if st.button("➕ Add Column", key="btn_add_col"):
            if not new_col_name.strip() or not formula_str:
                st.error("Fill all fields.")
            else:
                try:
                    if calc_type == "Extract Date/Time Part":
                        raw = df[date_col]

                        # ── Robust datetime parser (handles AM/PM and pure time strings) ──
                        def _parse_datetime_robust(series: pd.Series) -> pd.Series:
                            """
                            Multi-strategy parser that handles:
                              - ISO datetimes / dates  (2024-01-15, 2024-01-15 14:30)
                              - 12-hr time strings     (3:45 PM, 3:45:22 pm, 3:45PM)
                              - 24-hr time strings     (14:30, 07:06:11)
                              - Mixed columns          (some AM/PM, some 24-hr)
                            Returns a datetime64 Series; failures are NaT.
                            """
                            s = series.astype(str).str.strip()

                            # Strategy 1 — let pandas infer (handles ISO, many common formats)
                            result = pd.to_datetime(s, errors="coerce")

                            remaining = result.isna() & series.notna()
                            if not remaining.any():
                                return result

                            # Strategy 2 — normalise AM/PM spacing then retry
                            # "3:45PM" → "3:45 PM", "3:45pm" → "3:45 PM"
                            normalised = (
                                s[remaining]
                                .str.upper()
                                .str.replace(r"([AP]M)$", r" \1", regex=True)
                                .str.replace(r"\s{2,}", " ", regex=True)
                            )
                            result[remaining] = pd.to_datetime(
                                "1970-01-01 " + normalised, errors="coerce"
                            )

                            remaining = result.isna() & series.notna()
                            if not remaining.any():
                                return result

                            # Strategy 3 — explicit format sweep for common AM/PM patterns
                            for fmt in (
                                "%I:%M %p", "%I:%M:%S %p",
                                "%I:%M%p",  "%I:%M:%S%p",
                                "%I %p",
                                "%m/%d/%Y %I:%M %p", "%d/%m/%Y %I:%M %p",
                                "%m/%d/%Y %H:%M",    "%d/%m/%Y %H:%M",
                            ):
                                still = result.isna() & series.notna()
                                if not still.any():
                                    break
                                try:
                                    attempt = pd.to_datetime(
                                        s[still], format=fmt, errors="coerce"
                                    )
                                    result[still] = attempt
                                except Exception:
                                    pass

                            return result

                        temp_dates = _parse_datetime_robust(raw)

                        null_count = int(temp_dates.isna().sum())
                        if null_count:
                            st.warning(
                                f"⚠️ {null_count} value(s) in `{date_col}` could not be parsed "
                                f"as a date/time and will produce NaN in the new column."
                            )

                        mapping = {
                            "Year":              temp_dates.dt.year,
                            "Quarter":           temp_dates.dt.quarter,
                            "Month (Number)":    temp_dates.dt.month,
                            "Month Name":        temp_dates.dt.month_name(),
                            "Week Number":       temp_dates.dt.isocalendar().week.astype("Int64"),
                            "Day":               temp_dates.dt.day,
                            "Weekday Name":      temp_dates.dt.day_name(),
                            "Hour (12h AM/PM)":  temp_dates.dt.strftime("%-I %p"),
                            "Hour (24h)":        temp_dates.dt.hour,
                        }
                        df[new_col_name.strip()] = mapping[part_to_extract]
                    else:
                        df[new_col_name.strip()] = _safe_formula_eval(df, formula_str)
                    st.session_state.df = df
                    st.success(f"✅ Added {new_col_name.strip()}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    with tab_remove:
        col_to_del = st.selectbox("Select column to remove", df.columns.tolist(), key="col_to_del")
        confirm = st.checkbox(f"Confirm removal of **{col_to_del}**", key="confirm_del")
        if st.button("🗑️ Remove", key="btn_del_col", disabled=not confirm):
            df = df.drop(columns=[col_to_del])
            st.session_state.df = df
            for k in ["num_cols", "cat_cols"]:
                if k in st.session_state:
                    st.session_state[k] = [c for c in st.session_state[k] if c != col_to_del]
            st.success(f"✅ Removed {col_to_del}")
            st.rerun()

    return st.session_state.df
