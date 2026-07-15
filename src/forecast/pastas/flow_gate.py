"""Per-gauge ENS-driven admission gate for the low-flow Rivers layer — Stage 4
of ``docs/product/lowflow/build_plan.md``.

Pattern = ``src/forecast/pastas/screen.py::leakage_safe_hindcast`` (leakage-safe,
never raises, returns a dict of ``gate_pass`` + per-criterion numbers +
``reason``) but adapted to the two-pathway flow model
(``calibrate_flow``/``simulate_path``, ``docs/product/lowflow/analysis.md``
§3) and its own baseline/season/scoring rules (architecture decision 3):

  * Origins are drawn from the LOW-FLOW SEASON (Jun-Oct) only, spread over
    several distinct years — not "N days before the last observation" like
    the GW short-record gate, because the product's use-case (P(Q<Q95) within
    14 days) is a summer-recession question (analysis.md §5.5).
  * The baseline to beat is the NAIVE FIXED-RATE RECESSION from
    ``docs/product/lowflow/scripts/memory_skill_test.py`` /
    ``validation_fit.py`` (a single daily log-decline rate estimated from the
    training tail's median 14-day change, projected flat forward) — not
    persistence, not climatology.
  * Each origin is scored TWICE: CEILING (observed rainfall through the
    window — the upper bound analysis.md §5 calls "perfect-rain") and FLOOR
    (the window's rainfall replaced by day-of-year climatology computed from
    the gauge's own rain history up to that origin — the memory-only lower
    bound). One pastas fit per origin (via ``calibrate_flow``) drives BOTH
    scores via two ``simulate_path`` calls — no re-fitting, no bespoke model
    math.
  * The CEILING criteria trio decides ``gate_pass`` (fan-published at all);
    the FLOOR then decides the tier (the 2026-07-14 escalation resolution —
    see the threshold comments): a gauge that stays ROBUST on climatological
    rain (parity with the recession baseline, covered, and still sharp —
    ``_floor_robust``) is tier-1; a ceiling-pass that is not floor-robust is
    ``rain_dependent=True`` (publish wider, with a caveat; presentation is
    Stage 7's job, this module only sets the flag). Ceiling fail → status-only.
  * The same honest sigma-inflation mechanism as screen.py
    (``screen._inflation_factor``) widens each leg's band, pooled across
    origins, before the coverage criterion is evaluated — reused directly
    from ``screen``, not reimplemented, so both gates behave identically at
    the same confidence target.

Pure calibration/forecast reuse — imports pastas lazily via ``recharge``
(importing this module does not require pastas to be installed).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import recharge as R
from .screen import _EPS, _Z90, _inflation_factor, gate_pass

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
# CEILING (gate_pass — is the fan publishable at all?). The recession baseline
# is already a competent predictor (unlike GW screen.py's persistence
# baseline), so the go/no-go bar from the model spike ("must beat naive
# recession MEANINGFULLY", analysis.md §4) is a real skill margin, not a tie:
# skill_ratio = mean|obs-P50| / mean|obs-recession| must be <=0.80 (>=20% MAE
# improvement) — the exact threshold docs/product/lowflow/scripts/
# validation_fit.py used to grade analysis.md §5's "skill vs recession"
# column, so the live gate's tiering is directly comparable to that table.
MAX_SKILL_RATIO = 0.80
# Coverage / sharpness stay screen.py's defaults — same trio, same honesty
# philosophy: widen the band until it covers TARGET_COVERAGE_PCT of held-out
# obs (sigma_inflation), gate on a floor that only guards degenerate cases,
# and cap the WIDENED band as a fraction of the gauge's own log-flow range.
TARGET_COVERAGE_PCT = 80.0
MIN_COVERAGE_PCT = 50.0
MAX_BAND_FRAC = 0.60
# FLOOR (tier-1 vs rain_dependent — the resolved 2026-07-14 escalation).
# The floor's job is ROBUSTNESS, not value-add: "never worse than the best
# naive method, and still sharp, even if the rain forecast is worthless".
# analysis.md §5's +33/+45% memory-only floors were measured at YEAR-ROUND
# origins, where the fixed-rate recession is a weak baseline (winter rising
# limbs); at this gate's Jun-Oct origins the recession baseline is at its
# strongest, so demanding the ceiling's 20% margin of the floor is the wrong
# spec — parity is the honest ask:
#   FLOOR_MAX_SKILL_RATIO 1.05 — parity with recession within noise.
#   FLOOR_MAX_BAND_FRAC   0.20 — the widened 80% band spans at most a fifth
#     of the gauge's observed logQ range: still informative with zero rain
#     information. The 0.20 sits in the clean empirical gap between chalk
#     (<=0.16 on the validation 10) and the parity-floor flashy gauges
#     (>=0.29 — Mole 0.45, Medway 0.29: recession-parity skill but bands too
#     wide to lean on without a rain forecast — that's the point of the rule).
FLOOR_MAX_SKILL_RATIO = 1.05
FLOOR_MAX_BAND_FRAC = 0.20

MIN_TRAIN_ROWS = 1200       # ~3.3y — matches validation_fit.py's origin floor
MIN_EVAL_OBS = 10           # of the 14-day window, need this many held-out obs
RECESSION_LOOKBACK = 1200   # trailing obs used to estimate the recession rate
MIN_RECESSION_PAIRS = 30    # (d, d+14) pairs needed for a stable median rate
WINDOW = 14                 # the operational (P(Q<Q95) within...) horizon

# Low-flow-season (Jun-Oct) candidate origin dates, several per year so a
# single wet/dry summer doesn't dominate the verdict. Filtered to what each
# gauge's record can actually support (build_plan.md: >=6 origins over >=3
# distinct years) and capped for compute budget.
_ORIGIN_MONTH_DAYS: tuple[tuple[int, int], ...] = (
    (6, 15), (7, 15), (8, 15), (9, 15), (10, 15),
)
ORIGIN_CAP = 10
MIN_ORIGINS = 6
MIN_ORIGIN_YEARS = 3        # build_plan.md: >=6 origins over >=3 distinct years


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def _floor_robust(skill_ratio: float | None, cov: float | None,
                  band_frac: float | None, *,
                  max_skill_ratio: float = FLOOR_MAX_SKILL_RATIO,
                  min_coverage_pct: float = MIN_COVERAGE_PCT,
                  max_band_frac: float = FLOOR_MAX_BAND_FRAC) -> bool:
    """Tier-1 test on the climatological-rain (memory-only) leg: robustness,
    not value-add — parity with the recession baseline within noise
    (skill_ratio <= 1.05), covered after the honest widening, and a band still
    sharp enough to lean on (<= a fifth of the gauge's logQ range) even if the
    rain forecast is worthless. Any None (not evaluable) fails."""
    if skill_ratio is None or cov is None or band_frac is None:
        return False
    return bool(skill_ratio <= max_skill_ratio
                and cov >= min_coverage_pct
                and band_frac <= max_band_frac)

def _normalize_daily(s: pd.Series) -> pd.Series:
    s = s.copy()
    # .as_unit("ns") mirrors recharge._norm — a parquet-round-tripped shard
    # arrives with a datetime64[us] index, which pastas 1.14 silently
    # degenerates on (all-NaN residuals). Coerce before anything downstream.
    s.index = (pd.DatetimeIndex(pd.to_datetime(s.index))
               .as_unit("ns").tz_localize(None).normalize())
    return s[~s.index.duplicated(keep="last")].sort_index()


def _candidate_origins(index: pd.DatetimeIndex, *, min_train_rows: int,
                       window: int) -> list[pd.Timestamp]:
    """Low-flow-season origin dates this record can leakage-safely support:
    >=``min_train_rows`` obs strictly before the origin, and the ``window``-day
    evaluation horizon still inside the record. Spread across the whole usable
    span (evenly downsampled to ``ORIGIN_CAP``) so multi-year diversity is
    automatic rather than front- or back-loaded."""
    if len(index) == 0:
        return []
    start, end = index.min(), index.max()
    cands: list[pd.Timestamp] = []
    for year in range(start.year, end.year + 1):
        for month, day in _ORIGIN_MONTH_DAYS:
            try:
                origin = pd.Timestamp(year=year, month=month, day=day)
            except ValueError:                                    # pragma: no cover
                continue
            if origin < start or origin > end:
                continue
            if int((index <= origin).sum()) < min_train_rows:
                continue
            if origin + pd.Timedelta(days=window) > end:
                continue
            cands.append(origin)
    cands.sort()
    if len(cands) > ORIGIN_CAP:
        idx = sorted(set(np.linspace(0, len(cands) - 1, ORIGIN_CAP).round().astype(int)))
        cands = [cands[i] for i in idx]
    return cands


def _recession_baseline(logq_train: pd.Series, *, window: int) -> np.ndarray | None:
    """Naive fixed-rate recession, log space (memory_skill_test.py /
    validation_fit.py): a single daily log-decline rate estimated from the
    median ``window``-day change over the trailing ``RECESSION_LOOKBACK`` obs,
    projected flat forward from the last training observation. None if there
    aren't enough (d, d+window) pairs to estimate a stable rate."""
    tail = logq_train.iloc[-RECESSION_LOOKBACK:]
    decl = []
    for d in tail.index:
        d2 = d + pd.Timedelta(days=window)
        if d2 in logq_train.index:
            decl.append(float(logq_train[d2] - logq_train[d]))
    if len(decl) < MIN_RECESSION_PAIRS:
        return None
    kk = float(np.median(decl)) / window
    return float(logq_train.iloc[-1]) + kk * np.arange(1, window + 1)


# ---------------------------------------------------------------------------
# Per-origin scoring (never raises — returns None when not evaluable)
# ---------------------------------------------------------------------------

def _score_origin(gauge_id: str, q: pd.Series, prec: pd.Series, evap: pd.Series,
                  *, origin: pd.Timestamp, window: int) -> dict | None:
    """One leakage-safe low-flow-season origin: fit on ``q <= origin``, then
    score the next ``window`` days TWICE (ceiling = observed rain through the
    window, floor = day-of-year-climatology rain) against the recession
    baseline. Both legs share the ONE fit — ``simulate_path`` is called twice,
    ``calibrate_flow`` once."""
    cutoff = origin
    train = q[q.index <= cutoff]
    if len(train) < MIN_TRAIN_ROWS:
        return None
    try:
        rec = R.calibrate_flow(gauge_id, train, prec, evap, train_max=cutoff)
    except Exception:
        return None

    win = pd.date_range(cutoff + pd.Timedelta(days=1), periods=window, freq="D")
    eps = float(rec.get("eps", 0.001))
    logq = np.log(q.clip(lower=0) + eps)
    obs = logq.reindex(win).to_numpy(float)
    valid = np.isfinite(obs)
    n = int(valid.sum())
    if n < MIN_EVAL_OBS:
        return None
    o = obs[valid]

    try:
        mean_c, sig_c = R.simulate_path(rec, train, prec, evap, cutoff, win)
    except Exception:
        return None

    # Floor scenario: the window's rain is replaced by day-of-year climatology
    # computed from the gauge's OWN rain history up to the origin (leakage-safe
    # — no future rain informs the "memory-only" score).
    prec_train = prec[prec.index <= cutoff]
    if prec_train.empty:
        return None
    doy_clim = prec_train.groupby(prec_train.index.day_of_year).mean()
    fallback = float(prec_train.mean())
    prec_clim = prec.copy()
    for d in win:
        prec_clim.loc[d] = float(doy_clim.get(int(d.day_of_year), fallback))
    prec_clim = prec_clim.sort_index()

    try:
        mean_f, sig_f = R.simulate_path(rec, train, prec_clim, evap, cutoff, win)
    except Exception:
        return None

    logq_train = logq[logq.index <= cutoff]
    base = _recession_baseline(logq_train, window=window)
    if base is None:
        return None
    mae_rec = float(np.abs(o - base[valid]).mean())
    if mae_rec < _EPS:
        return None

    def _leg(mean: np.ndarray, sig: np.ndarray) -> dict:
        m = np.asarray(mean, float)[valid]
        s = np.clip(np.asarray(sig, float)[valid], _EPS, None)
        mae = float(np.abs(o - m).mean())
        p10, p90 = m - _Z90 * s, m + _Z90 * s
        cov = 100.0 * float(((o >= p10) & (o <= p90)).sum()) / n
        band = float(np.mean(p90 - p10))
        # skill_ratio is deliberately NOT computed per-origin here — it's
        # aggregated as mean(mae)/mean(mae_recession) (ratio-of-means, matching
        # screen.py's skill_ratio) in admit_gauge, which is far less sensitive
        # to a single origin with an anomalously small recession-baseline MAE
        # than averaging per-origin ratios would be.
        return {"mae": round(mae, 4), "cov": round(cov, 1), "band": round(band, 4),
                "absz": np.abs(o - m) / s}

    return {"origin": cutoff.date().isoformat(), "n_train": int(len(train)),
            "n_eval": n, "mae_recession": round(mae_rec, 4),
            "ceiling": _leg(mean_c, sig_c), "floor": _leg(mean_f, sig_f)}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def admit_gauge(gauge_id: str, q: pd.Series, prec: pd.Series, evap: pd.Series,
                *, window: int = WINDOW, min_origins: int = MIN_ORIGINS,
                target_coverage_pct: float = TARGET_COVERAGE_PCT,
                min_coverage_pct: float = MIN_COVERAGE_PCT,
                max_skill_ratio: float = MAX_SKILL_RATIO,
                max_band_frac: float = MAX_BAND_FRAC) -> dict:
    """Backtest ``window``-day flow forecasts at several low-flow-season
    origins spread over multiple years, and gate the (sigma-inflated) fan on
    skill vs the naive recession baseline, coverage and sharpness — scored
    once with observed rain (ceiling) and once with climatological rain
    (floor). The ceiling trio decides ``gate_pass``; the floor's robustness
    test (``_floor_robust``) decides tier-1 vs rain_dependent.

    Returns a JSON-serialisable dict (never raises — a gate failure must not
    abort a fleet scan):
      gate_pass, tier ("tier1" | "rain_dependent" | "status_only"),
      rain_dependent, n_origins, n_years, range_logq, floor, ceiling
      (each a {sigma_inflation, cov14, mae14, skill_ratio, band_frac} dict —
      the ceiling adds "gate_pass", the floor adds "robust" — or None if not
      evaluable), origins (per-origin detail), reason.
    """
    out = {"gauge_id": gauge_id, "gate_pass": False, "tier": "status_only",
           "rain_dependent": False, "n_origins": 0, "n_years": 0,
           "range_logq": None, "floor": None, "ceiling": None, "origins": [],
           "window": int(window), "reason": ""}
    try:
        qn = _normalize_daily(q).dropna()
        if qn.empty:
            out["reason"] = "no_obs"
            return out
        positive = qn[qn > 0]
        eps_full = max(0.001, float(positive.min()) / 10) if len(positive) else 0.001
        logq_full = np.log(qn.clip(lower=0) + eps_full)
        rng = float(logq_full.max() - logq_full.min())
        out["range_logq"] = round(rng, 4)

        prec_n = _normalize_daily(prec)
        if prec_n.empty:
            out["reason"] = "no_rain"
            return out

        candidates = _candidate_origins(qn.index, min_train_rows=MIN_TRAIN_ROWS,
                                        window=window)
        per = []
        for origin in candidates:
            s = _score_origin(gauge_id, qn, prec_n, evap, origin=origin, window=window)
            if s is not None:
                per.append(s)
        out["n_origins"] = len(per)
        out["n_years"] = len({pd.Timestamp(p["origin"]).year for p in per})
        if len(per) < min_origins:
            out["origins"] = per
            out["reason"] = f"origins<{min_origins}"
            return out
        if out["n_years"] < MIN_ORIGIN_YEARS:
            # >=3 distinct origin years (build_plan.md) — 6 origins packed
            # into 2 summers is one wet/dry-pair verdict, not a robust one.
            out["origins"] = per
            out["reason"] = f"origin_years<{MIN_ORIGIN_YEARS}"
            return out

        mae_rec_agg = float(np.mean([p["mae_recession"] for p in per]))

        def _aggregate(leg: str) -> dict:
            allz = np.concatenate([np.asarray(p[leg]["absz"], float) for p in per])
            k = _inflation_factor(allz, target_coverage_pct / 100.0)
            cov = 100.0 * float((allz <= k * _Z90).mean())
            mae = float(np.mean([p[leg]["mae"] for p in per]))
            skill_ratio = mae / max(mae_rec_agg, _EPS)
            band = k * float(np.mean([p[leg]["band"] for p in per]))
            band_frac = band / rng if rng > 0 else float("inf")
            return {"sigma_inflation": round(k, 3), "cov14": round(cov, 1),
                    "mae14": round(mae, 4), "skill_ratio": round(skill_ratio, 3),
                    "band_frac": round(band_frac, 3)}

        floor_res = _aggregate("floor")
        ceiling_res = _aggregate("ceiling")
        for p in per:
            p["floor"].pop("absz", None)
            p["ceiling"].pop("absz", None)

        # The CEILING trio is the admission gate (is the fan publishable at
        # all, given a good rain forecast?); the FLOOR's robustness test picks
        # tier-1 (2026-07-14 escalation resolution — see threshold comments).
        ceiling_res["gate_pass"] = gate_pass(
            ceiling_res["cov14"], ceiling_res["skill_ratio"],
            ceiling_res["band_frac"], min_coverage_pct=min_coverage_pct,
            max_skill_ratio=max_skill_ratio, max_band_frac=max_band_frac)
        floor_res["robust"] = _floor_robust(
            floor_res["skill_ratio"], floor_res["cov14"],
            floor_res["band_frac"], min_coverage_pct=min_coverage_pct)

        out["origins"] = per
        out["floor"] = floor_res
        out["ceiling"] = ceiling_res

        if ceiling_res["gate_pass"] and floor_res["robust"]:
            out["gate_pass"] = True
            out["tier"] = "tier1"
            out["rain_dependent"] = False
            out["reason"] = "pass_floor_robust"
        elif ceiling_res["gate_pass"]:
            out["gate_pass"] = True
            out["tier"] = "rain_dependent"
            out["rain_dependent"] = True
            out["reason"] = "pass_ceiling_only"
        else:
            out["gate_pass"] = False
            out["tier"] = "status_only"
            out["rain_dependent"] = False
            out["reason"] = (
                f"ceiling: skill={ceiling_res['skill_ratio']:.2f} "
                f"cov={ceiling_res['cov14']:.0f}% band/rng={ceiling_res['band_frac']:.0%} | "
                f"floor: skill={floor_res['skill_ratio']:.2f} "
                f"cov={floor_res['cov14']:.0f}% band/rng={floor_res['band_frac']:.0%}")
        return out
    except Exception as exc:                                       # pragma: no cover
        out["reason"] = f"gate_error:{type(exc).__name__}"
        return out
