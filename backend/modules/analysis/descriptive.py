"""
modules/analysis/descriptive.py -- Descriptive statistics table runner.
======================================================================

Renders a pandas describe() summary table directly into the Streamlit page
for all numeric columns in the dataset.

Unlike every other runner, this module renders inline via st.dataframe()
rather than returning Plotly figures. It always returns an empty list [].

The caller (pages/analysis.py) handles the empty return gracefully --
no "no charts generated" warning is shown for this analysis type.
"""

import streamlit as st
from modules.charts import num_cols as _num_cols


def run_descriptive(df):
    """
    Render a descriptive statistics table for all numeric columns.

    Columns in the output table:
        Column, Count, Mean, Std, Min, 25%, 50%, 75%, Max

    Args:
        df: Working DataFrame.

    Returns:
        [] -- no Plotly figures; rendering is done inline via st.dataframe().
    """
    num = _num_cols()
    if not num:
        st.warning("No numeric columns found. Check your column classification on the upload page.")
        return []

    # Build the transposed describe table -- rows = columns, cols = stats.
    desc = df[num].describe().T.reset_index()

    # Capitalise all column headers; rename the index column to "Column".
    desc.columns = [c.title() if c != "index" else "Column" for c in desc.columns]

    # Round all numeric values to 4 decimal places for readable display.
    desc[desc.select_dtypes("number").columns] = desc.select_dtypes("number").round(4)

    st.dataframe(desc, use_container_width=True, hide_index=True)
    return []
