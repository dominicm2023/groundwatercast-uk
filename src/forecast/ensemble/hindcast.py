"""Perfect-forecast hindcast harness — resolves the §6 GW-roll decision.

We re-forecast past dates driving each roll method with the *observed* rainfall
/ recharge over the forecast window (perfect-forecast), which isolates GW-roll
skill from weather error. Methods are scored by per-lead-day MAE and bias drift
against observed GW. Leakage-safe: methods are fit on the TRAIN split;
hindcast origins are drawn from the TEST split.

Note (risk-index retirement): the original harness also scored an
``rf_guarded`` random-forest baseline — that left with ``src.model``. The
historical decision it informed (→ ``reduced_form_ar``) is recorded in the
design doc's amendments log; this harness remains for revalidating the roll
choice on new regions, with persistence as the skill floor.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.features.io import load_features
from src.forecast.ensemble import gw_roll

ROOT = Path(__file__).parents[3]


def _split_train_test(df: pd.DataFrame, ratio: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Strict time-based split (no shuffling) — kept local since the RF
    model module that used to own it is retired."""
    df_sorted = df.sort_index()
    cutoff = int(len(df_sorted) * ratio)
    return df_sorted.iloc[:cutoff], df_sorted.iloc[cutoff:]


def _select_pilot_stations(test: pd.DataFrame, n: int) -> list[str]:
    """Top-n stations by test-split row count (best-covered = most origins)."""
    counts = test["station_id"].value_counts()
    return counts.head(n).index.tolist()


def _origins(station_rows: pd.DataFrame, test_start: pd.Timestamp,
             horizon: int, stride: int, max_origins: int) -> list[int]:
    """Positional indices in station_rows that can seed a horizon-length roll
    entirely within the test period (and have ≥7 rows of prior context)."""
    idx = station_rows.index
    n = len(station_rows)
    out = []
    for i in range(7, n - horizon):
        if idx[i] < test_start:
            continue
        if len(out) and (i - out[-1]) < stride:
            continue
        out.append(i)
        if len(out) >= max_origins:
            break
    return out


# Origin regime by the mean observed ΔGW over the prior 7 days (uses only
# info available at the origin — no leakage):
#   > _STRONG_THRESHOLD            → "strong"   (fast active recharge; breach-critical)
#   > _RISE_THRESHOLD              → "rising"   (mild recharge)
#   else                          → "quiescent"
_RISE_THRESHOLD_M_PER_DAY = 0.02
_STRONG_THRESHOLD_M_PER_DAY = 0.10


def _regime(recent_dgw: float) -> str:
    if recent_dgw > _STRONG_THRESHOLD_M_PER_DAY:
        return "strong"
    if recent_dgw > _RISE_THRESHOLD_M_PER_DAY:
        return "rising"
    return "quiescent"


def run_hindcast(config: dict, *, n_stations: int = 12, horizon: int = 14,
                 stride: int = 15, max_origins_per_station: int = 14,
                 seed: int = 42) -> dict:
    df, _ = load_features(config)
    ratio = float(config.get("forecast", {}).get("hindcast_split_ratio", 0.8))
    train, test = _split_train_test(df, ratio)
    test_start = test.index.min()

    pilots = _select_pilot_stations(test, n_stations)
    print(f"Pilot stations ({len(pilots)}): "
          + ", ".join(s[:8] for s in pilots))

    methods = ["persistence", "reduced_form", "reduced_form_ar",
               "reduced_form_cr"]
    records: list[dict] = []          # {method, regime, lead, err}
    n_rolls = {"strong": 0, "rising": 0, "quiescent": 0}

    for sid in pilots:
        s_all = df[df["station_id"] == sid].sort_index()
        s_train = s_all[s_all.index < test_start]
        if len(s_train) < 60:
            continue
        rf_params = gw_roll.fit_reduced_form(s_train)
        ar_params = gw_roll.fit_reduced_form_ar(s_train)
        cr_params = gw_roll.fit_reduced_form_cr(s_train)
        dgw_clip, gw_clip = gw_roll.station_guardrails(s_train)
        dgw_all = s_all["GW_Level"] - s_all["GW_Lag1"]

        for i in _origins(s_all, test_start, horizon, stride,
                          max_origins_per_station):
            seed_gw = float(s_all.iloc[i]["GW_Level"])
            seed_dgw = float(dgw_all.iloc[i])
            recent = float(dgw_all.iloc[i - 6: i + 1].mean())
            regime = _regime(recent)
            fut = s_all.iloc[i + 1: i + 1 + horizon]
            actual = fut["GW_Level"].to_numpy(dtype=float)
            if np.isnan(actual).any():
                continue

            preds = {
                "persistence": np.full(horizon, seed_gw),
                "reduced_form": gw_roll.roll_reduced_form(
                    seed_gw, fut, rf_params, gw_clip=gw_clip),
                "reduced_form_ar": gw_roll.roll_reduced_form_ar(
                    seed_gw, seed_dgw, fut, ar_params,
                    dgw_clip=dgw_clip, gw_clip=gw_clip),
                "reduced_form_cr": gw_roll.roll_reduced_form_cr(
                    seed_gw, seed_dgw, fut, cr_params,
                    dgw_clip=dgw_clip, gw_clip=gw_clip),
            }
            for m in methods:
                for lead in range(horizon):
                    records.append({"method": m, "regime": regime,
                                    "lead": lead + 1,
                                    "err": preds[m][lead] - actual[lead]})
            n_rolls[regime] += 1

    rec = pd.DataFrame(records)
    rec["ae"] = rec["err"].abs()
    summary = _summarise(rec, horizon)
    decision = _decide(summary)

    total = sum(n_rolls.values())
    print(f"\nRolls scored: {total}  (strong {n_rolls['strong']}, "
          f"rising {n_rolls['rising']}, quiescent {n_rolls['quiescent']}; "
          f"horizon {horizon} d)")
    _print_table(summary, decision)
    _write_report(rec, summary, decision, n_rolls, horizon)
    return {"summary": summary, "decision": decision, "n_rolls": n_rolls}


def _summarise(rec: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Per-method overall + per-regime MAE, plus @14d MAE and bias drift."""
    rows = []
    for m, g in rec.groupby("method"):
        overall = float(g["ae"].mean())
        by_reg = g.groupby("regime")["ae"].mean()
        lead14 = g[g["lead"] == horizon]["ae"].mean()
        # bias drift = slope of mean signed error vs lead day
        bias_by_lead = g.groupby("lead")["err"].mean()
        slope = (np.polyfit(bias_by_lead.index, bias_by_lead.values, 1)[0]
                 if len(bias_by_lead) > 1 else 0.0)
        rows.append({
            "method": m,
            "mae_overall": overall,
            "mae_strong": float(by_reg.get("strong", np.nan)),
            "mae_rising": float(by_reg.get("rising", np.nan)),
            "mae_quiescent": float(by_reg.get("quiescent", np.nan)),
            "mae_lead14": float(lead14),
            "bias_slope": float(slope),
        })
    return pd.DataFrame(rows).sort_values("mae_overall").reset_index(drop=True)


# Combined-score weights (overall, strong, rising) — shared by _decide and the
# markdown report so the two can't drift apart.
_DECIDE_WEIGHTS = (0.34, 0.33, 0.33)


def _decide(summary: pd.DataFrame) -> dict:
    """Winner = lowest combined score among the roll methods (persistence is
    the floor). The combined score weights the breach-critical regimes:
    _DECIDE_WEIGHTS = overall/strong/rising (a regime's own overall MAE
    substitutes when that regime had no samples)."""
    s = summary.set_index("method")
    cand = [m for m in ("reduced_form", "reduced_form_ar",
                        "reduced_form_cr") if m in s.index]
    if not cand:
        return {"winner": None, "reason": "no candidate methods scored"}

    def score(m):
        ov = s.loc[m, "mae_overall"]
        strong = s.loc[m, "mae_strong"]
        strong = ov if np.isnan(strong) else strong
        rising = s.loc[m, "mae_rising"]
        rising = ov if np.isnan(rising) else rising
        w_ov, w_st, w_ri = _DECIDE_WEIGHTS
        return w_ov * ov + w_st * strong + w_ri * rising

    best = min(cand, key=score)
    w_ov, w_st, w_ri = _DECIDE_WEIGHTS
    reason = (f"{best} — lowest combined {w_ov}·overall+{w_st}·strong+{w_ri}·rising. "
              f"overall {s.loc[best, 'mae_overall']:.3f}, "
              f"strong {s.loc[best, 'mae_strong']:.3f}, "
              f"rising {s.loc[best, 'mae_rising']:.3f}, "
              f"quiescent {s.loc[best, 'mae_quiescent']:.3f} mAOD.")
    return {"winner": best, "reason": reason, "config_value": best}


def _print_table(summary, decision) -> None:
    print("\nMAE by regime (mAOD):")
    print(f"  {'method':<16} {'overall':>8} {'strong':>8} {'rising':>8} "
          f"{'quiescent':>10} {'@14d':>7} {'biasSlope':>10}")
    for _, r in summary.iterrows():
        print(f"  {r['method']:<16} {r['mae_overall']:>8.3f} "
              f"{r['mae_strong']:>8.3f} {r['mae_rising']:>8.3f} "
              f"{r['mae_quiescent']:>10.3f} {r['mae_lead14']:>7.3f} "
              f"{r['bias_slope']:>+10.4f}")
    print(f"\nDECISION → {decision['winner']}")
    print(f"  {decision['reason']}")


def _write_report(rec, summary, decision, n_rolls, horizon) -> None:
    out_csv = ROOT / "outputs" / "ensemble_hindcast.csv"
    out_md = ROOT / "outputs" / "ensemble_hindcast.md"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    # per (method, lead, regime) MAE/bias for downstream plots
    per = (rec.groupby(["method", "regime", "lead"])
           .agg(mae=("ae", "mean"), bias=("err", "mean")).reset_index())
    per.to_csv(out_csv, index=False)

    lines = [
        "# Ensemble GW-roll hindcast — regime-stratified (design §6 + Phase 2.5)",
        "",
        f"Perfect-forecast hindcast, horizon {horizon} d. Rolls: "
        f"{n_rolls['strong']} strong + {n_rolls['rising']} rising + "
        f"{n_rolls['quiescent']} quiescent.",
        "Leakage-safe: methods fit on train split, origins from test split. "
        "Regime = mean ΔGW over the prior 7 days: "
        f"> {_STRONG_THRESHOLD_M_PER_DAY} → 'strong' (fast recharge, "
        f"breach-critical); > {_RISE_THRESHOLD_M_PER_DAY} → 'rising' (mild).",
        "",
        "## MAE by regime (mAOD)",
        "",
        "| method | overall | strong | rising | quiescent | @14d | bias slope |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in summary.iterrows():
        lines.append(
            f"| {r['method']} | {r['mae_overall']:.3f} | {r['mae_strong']:.3f} "
            f"| {r['mae_rising']:.3f} | {r['mae_quiescent']:.3f} "
            f"| {r['mae_lead14']:.3f} | {r['bias_slope']:+.4f} |")
    lines += [
        "",
        f"**Decision → `{decision['winner']}`** (combined "
        f"{_DECIDE_WEIGHTS[0]}·overall + {_DECIDE_WEIGHTS[1]}·strong + "
        f"{_DECIDE_WEIGHTS[2]}·rising)  ",
        decision["reason"],
        "",
        "Set `config.forecast.ensemble.gw_roll_method` accordingly.",
    ]
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport → {out_md.relative_to(ROOT)} (+ .csv)")
