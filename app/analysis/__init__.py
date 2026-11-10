"""Technical analysis and stock discovery.

Public entry points:
    from app.analysis import load_profile, run_pipeline, StockContext, StockMeta
"""

from app.analysis.context import StockContext, StockMeta
from app.analysis.pipeline import PipelineResult, load_profile, run_pipeline

__all__ = ["PipelineResult", "StockContext", "StockMeta", "load_profile", "run_pipeline"]
