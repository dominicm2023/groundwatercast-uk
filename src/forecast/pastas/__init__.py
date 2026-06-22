"""Calibrated Pastas TFN recharge for groundwater forecasting (production).

A calibrated transfer-function-noise model with a non-linear PET-driven
``FlexModel`` recharge beats the ``reduced_form_ar`` roll overall and at the
14-day horizon, and the gain survives real forecast error (see
``outputs/pastas_vs_ar_*.md``).

ENVIRONMENT: Pastas (numba/llvmlite/scipy) is **isolated in a dedicated venv** and
is imported lazily inside functions, so importing this package never burdens the
main GW-pipeline environment. The calibration driver and tests run under the
pastas venv; the main pipeline only ever reads the cached artefacts.
"""
