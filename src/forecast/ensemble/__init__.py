"""Probabilistic ensemble rainfall providers.

See docs/ensemble_forecast_design.md for the design. The public surface is the
provider abstraction and a factory; concrete providers are imported lazily so
optional heavy deps (cfgrib/eccodes) are only required when actually used.
"""
from .provider import (
    EnsembleRainfallProvider,
    OUTPUT_COLUMNS,
    get_provider,
)

__all__ = ["EnsembleRainfallProvider", "OUTPUT_COLUMNS", "get_provider"]
