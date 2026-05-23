"""
modules/analysis/runners.py -- Re-exports all runner functions.

Kept for backward compatibility. New code should import directly from
the individual analysis modules or from modules.analysis.
"""

from modules.analysis.descriptive  import run_descriptive   # noqa: F401
from modules.analysis.statistical  import run_statistical   # noqa: F401
from modules.analysis.distribution import run_distribution  # noqa: F401
from modules.analysis.correlation  import run_correlation   # noqa: F401
from modules.analysis.categorical  import run_categorical   # noqa: F401
from modules.analysis.pie_chart    import run_pie_chart     # noqa: F401
from modules.analysis.time_series  import run_time_series   # noqa: F401
from modules.analysis.data_quality import run_data_quality  # noqa: F401
from modules.analysis.outlier      import run_outlier       # noqa: F401
from modules.analysis.scatter_plot import run_scatter_plot  # noqa: F401
from modules.analysis.matrix_table import run_matrix_table  # noqa: F401
from modules.analysis.map_plot     import run_map_plot      # noqa: F401
