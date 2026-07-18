"""Abstraction-influenced-site screen (roadmap H7, report-only).

RE-ENABLED 2026-07-18 behind the **licence-proximity gate**: amplitude evidence
now only flags a site where the H7 capture-zone screen
(``src.diagnostics.abstraction_influence`` → ``abstraction_influence.csv``)
puts a licensed groundwater abstraction within its volume-banded radius —
a proximity *prior* multiplying the amplitude *evidence*. This closes the
2026-06-17 negative result: ungated, the metric over-flagged natural
high-amplitude Chalk (575 evaluated → 125 flagged at 25 km / 51 at 6 km)
because the 3-value aquifer class (Principal/Secondary/Low) can't separate
downland Chalk (10–30 m natural swing) from valley/confined Chalk. The licence
prior is exactly the external covariate that finding asked for. Caveats travel:
licence proximity means *licensed capacity* nearby, not observed pumping, and
the extract covers >100 m³/day returns-submitting licences only. The
**register-reason path (``abstraction_influenced`` in known_bad_stations.yaml)
remains the only thing that excludes a site**. See
``docs/abstraction_screen_design.md``.


HOUK deliberately excludes heavily-pumped boreholes: at a site dominated by local
abstraction the level reflects a pump schedule, not the aquifer's response to
recharge, so a recharge-driven forecast is unreliable there. The *monotonic* form
of that influence (a sustained, isolated, rainfall-incoherent decline) is already
routed to ``review_exclude`` by the trend screen. This module targets the part the
trend screen misses — the **cyclic / seasonal pumping** case — using the one signal
that survives daily-mean aggregation:

  **excess seasonal amplitude vs same-aquifer-class neighbours.**

Heavy summer abstraction adds an annual drawdown-recovery swing on top of the
natural recession, so a pumped borehole swings markedly *more* than nearby
boreholes on the same aquifer (which share the regional climate / recession). The
comparison is restricted to same-aquifer-class neighbours so a naturally
high-amplitude Chalk borehole isn't flagged just for being Chalk among Sandstone.

Like the trend screen, this **reports and a human acts** — it never excludes a
station. Confirmed sites are added to ``data/external/known_bad_stations.yaml`` by
hand (reason ``abstraction_influenced``), which ``exclusions.py`` already honours.

IMPORTANT (honesty): this is an **advisory candidate flag, not a verdict**. Daily
means smooth out sub-daily pump cycles, and natural recession can mimic part of the
signal, so the screen surfaces sites for a metadata / abstraction-licence check
(``recommended_action="metadata_check"``). Real-world thresholds want calibration
against confirmed abstraction sites once ground truth exists; the unit tests pin
the metric's behaviour on controlled synthetic series in the meantime.

Pure numpy/pandas (never imports pastas). Per-borehole metrics (seasonal amplitude,
record length, rainfall coherence) are reused from ``trend_screen.screen_series``;
I/O lives in ``scripts/build_abstraction_screen.py``.
"""
from __future__ import annotations

import numpy as np

from src.diagnostics.abstraction_influence import tier_at_least

_SEV_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
_SEV_DOWN = {"high": "medium", "medium": "low", "low": "low"}


def amplitude_isolation(subject_amp: float, neighbour_amps, cfg: dict) -> dict:
    """Compare a borehole's seasonal amplitude to its neighbours'.

    ``amp_ratio`` = subject amplitude / median neighbour amplitude. Classes:
      - ``excess``       — ratio >= ``amp_ratio_min`` and the swing is real
                           (subject amp >= ``min_amp_m``): an abstraction candidate.
      - ``regional``     — comparable to neighbours (within [1/min, min]): the
                           seasonality is shared, i.e. climate-driven, not local.
      - ``muted``        — subject swings *less* than neighbours.
      - ``no_neighbours``— too few valid same-class neighbours to judge.
    """
    nb = cfg.get("neighbour", {})
    min_n = int(nb.get("min_neighbours", 2))
    ratio_min = float(cfg["amp_ratio_min"])
    min_amp = float(cfg.get("min_amp_m", 0.0))

    amps = [float(a) for a in neighbour_amps if a is not None and np.isfinite(a) and a > 0]
    if len(amps) < min_n or not (np.isfinite(subject_amp) and subject_amp > 0):
        return dict(amplitude_isolation_class="no_neighbours",
                    neighbour_count=len(amps), neighbour_median_amp=np.nan,
                    amp_ratio=np.nan)
    med = float(np.median(amps))
    amp_ratio = subject_amp / med if med > 1e-9 else np.nan

    if not np.isfinite(amp_ratio):
        cls = "no_neighbours"
    elif amp_ratio >= ratio_min and subject_amp >= min_amp:
        cls = "excess"
    elif amp_ratio < (1.0 / ratio_min):
        cls = "muted"
    else:
        cls = "regional"
    return dict(amplitude_isolation_class=cls, neighbour_count=len(amps),
                neighbour_median_amp=med, amp_ratio=amp_ratio)


def classify(m: dict, cfg: dict) -> dict:
    """Severity + provenance_class + recommended_action from the metric dict.

    Only an ``excess``-amplitude, sufficiently-long record is flagged, and only
    ever for a human ``metadata_check`` — never auto-exclusion. Severity scales
    with how far the swing exceeds the neighbourhood (``amp_ratio``).

    With ``cfg["licence_gate"]`` enabled, ``m["influence_tier"]`` (from the H7
    capture-zone screen) acts as the proximity prior: an excess-amplitude site
    with no licensed groundwater abstraction in range is recorded as
    ``excess_amplitude_no_licence`` (severity none — the very cohort the
    ungated 2026-06-17 run over-flagged), and a merely-``possible`` site is
    downgraded one severity notch vs ``likely``. A missing tier is treated as
    ``none``: no licence evidence, no flag."""
    iso = m.get("amplitude_isolation_class", "no_neighbours")
    ratio = m.get("amp_ratio", np.nan)
    years = m.get("record_years", np.nan)
    min_years = float(cfg.get("min_years", 0.0))

    enough_record = np.isfinite(years) and years >= min_years
    if iso != "excess" or not enough_record or not np.isfinite(ratio):
        return dict(severity="none", provenance_class="not_abstraction_suspect",
                    recommended_action="none")

    gate = cfg.get("licence_gate", {})
    tier = m.get("influence_tier") or "none"
    if gate.get("enabled", False):
        if not tier_at_least(tier, gate.get("min_tier", "possible")):
            return dict(severity="none",
                        provenance_class="excess_amplitude_no_licence",
                        recommended_action="none")

    if ratio >= float(cfg["amp_ratio_high"]):
        sev = "high"
    elif ratio >= float(cfg["amp_ratio_med"]):
        sev = "medium"
    else:
        sev = "low"
    if (gate.get("enabled", False) and gate.get("downgrade_possible", True)
            and tier == "possible"):
        sev = _SEV_DOWN[sev]
    return dict(severity=sev, provenance_class="abstraction_suspect",
                recommended_action="metadata_check")


def passes_severity(severity: str, emit_min: str) -> bool:
    """True if ``severity`` meets the configured ``emit_min_severity`` floor."""
    return _SEV_RANK.get(severity, 0) >= _SEV_RANK.get(emit_min, 1)
