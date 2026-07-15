"""A/B report: calendar-true (fixed) vs positional (pre-fix) lag / rolling /
Weibull feature computation, run against the REAL local fleet.

BUGS.md: "Lag / rolling / Weibull features computed positionally over a
gap-collapsed daily index, mislabelling temporal distance" — this is the
merge-gate evidence for that fix (src/features/build.py: reindex_to_calendar,
create_features, apply_weibull_recharge).

The OLD path below is a FROZEN, verbatim copy of src/features/build.py as of
origin/main before the fix (no calendar reindex; strict all-or-nothing
Weibull NaN handling) — kept inline rather than via git plumbing so this
script stays runnable standalone. resample_to_daily / join_timeseries /
average_rainfall / clean_groundwater_series are UNCHANGED by the fix and are
imported directly from the current module for both paths.

Usage:
    python -m scripts.ab_gap_features            # feature-table diff only
    python -m scripts.ab_gap_features --roll      # + roll_p50 diff (reuses
                                                     the cached
                                                     forecast_ensemble_members.parquet
                                                     and gw_by_station shards
                                                     -- no network fetch)

Writes:
    outputs/ab_gap_features.md         (this report, markdown + tables)
    outputs/ab_gap_features_detail.csv (per-station diff detail)
    outputs/ab_gap_features_roll.csv   (per-station roll_p50 diff, --roll only)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.build import (  # noqa: E402
    _GROUP_CODES,
    average_rainfall,
    clean_groundwater_series,
    compute_weibull_kernel,
    create_features as NEW_create_features,
    get_station_group,
    join_timeseries,
    load_timeseries,
    resample_to_daily,
)

# Tolerance for the "zero gaps -> zero diff" equivalence assertion. Floating-
# point roundtrip through pandas rolling/OLS is not bit-exact across the two
# code paths' slightly different call order, so use a tight numeric tolerance
# rather than requiring literal ==.
_EQUIV_ATOL = 1e-9


# ---------------------------------------------------------------------------
# OLD (pre-fix) path -- frozen, not imported. Positional: shift()/rolling()
# run directly on the gap-collapsed input index, no calendar reindex first;
# Weibull recharge is strict all-or-nothing (any NaN in the lag window -> NaN).
# ---------------------------------------------------------------------------

def OLD_apply_weibull_recharge(rainfall: pd.Series, kernel: np.ndarray) -> pd.Series:
    lag_days = len(kernel)
    shifted = rainfall.shift(1)
    return shifted.rolling(window=lag_days, min_periods=lag_days).apply(
        lambda x: float(np.dot(x[::-1], kernel)), raw=True,
    )


def OLD_create_features(
    df: pd.DataFrame,
    weibull_cfg: dict | None = None,
    weibull_multi_cfg: dict | None = None,
    weibull_by_group_cfg: dict | None = None,
    region_group: str = "unknown",
) -> pd.DataFrame:
    df = df.copy()
    dates = df.index.tz_convert("UTC").normalize()

    df["GW_Lag1"] = df["GW_Level"].shift(1)
    df["GW_Lag7"] = df["GW_Level"].shift(7)
    df["GW_Lag30"] = df["GW_Level"].shift(30)

    df["Rain_1d_sum"] = df["Rainfall"].rolling(1).sum()
    df["Rain_3d_sum"] = df["Rainfall"].rolling(3).sum()
    df["Rain_7d_sum"] = df["Rainfall"].rolling(7).sum()

    doy = dates.day_of_year.values
    df["day_of_year"] = doy
    df["Sin_DOY"] = np.sin(2 * np.pi * doy / 365.25)
    df["Cos_DOY"] = np.cos(2 * np.pi * doy / 365.25)

    required_dropna = [
        "GW_Lag1", "GW_Lag7", "GW_Lag30",
        "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum",
    ]

    if weibull_cfg is not None and weibull_cfg.get("enabled", False):
        kernel = compute_weibull_kernel(
            float(weibull_cfg["k"]), float(weibull_cfg["lambda"]), int(weibull_cfg["lag_days"]))
        df["Recharge_Weibull"] = OLD_apply_weibull_recharge(df["Rainfall"], kernel)
        required_dropna.append("Recharge_Weibull")

    if weibull_multi_cfg is not None and weibull_multi_cfg.get("enabled", False):
        add_masked = weibull_multi_cfg.get("add_masked", True)
        for name, kern_cfg in weibull_multi_cfg.get("kernels", {}).items():
            mkernel = compute_weibull_kernel(
                float(kern_cfg["k"]), float(kern_cfg["lambda"]), int(kern_cfg["lag_days"]))
            recharge = OLD_apply_weibull_recharge(df["Rainfall"], mkernel)
            if add_masked:
                masked_col = f"Recharge_{name.capitalize()}_masked"
                df[masked_col] = np.where(region_group == name, recharge, 0.0)
                if region_group == name:
                    required_dropna.append(masked_col)
        df["region_group_code"] = _GROUP_CODES.get(region_group, 2)

    if weibull_by_group_cfg is not None and weibull_by_group_cfg.get("enabled", False):
        kern_cfgs = {k: v for k, v in weibull_by_group_cfg.items() if k != "enabled"}
        kern_cfg = kern_cfgs.get(region_group, next(iter(kern_cfgs.values())))
        bgkernel = compute_weibull_kernel(
            float(kern_cfg["k"]), float(kern_cfg["lambda"]), int(kern_cfg["lag_days"]))
        df["Recharge_Weibull"] = OLD_apply_weibull_recharge(df["Rainfall"], bgkernel)
        required_dropna.append("Recharge_Weibull")
        df["region_group_code"] = _GROUP_CODES.get(region_group, 2)

    df = df.dropna(subset=required_dropna)
    return df


# ---------------------------------------------------------------------------
# Fleet loop: build the joined table once per station, feature-engineer it
# BOTH ways.
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    return json.loads((ROOT / "config" / "config.json").read_text())


def _build_joined(row: pd.Series, raw_root: Path, iqr_fence: float,
                  min_daily_std: float) -> pd.DataFrame | None:
    gw_measure_id = str(row["GWMeasureID"])
    gw_raw = load_timeseries(gw_measure_id, "groundwater", str(raw_root))
    if gw_raw is None:
        return None
    gw_raw, _ = clean_groundwater_series(gw_raw, iqr_fence)
    gw_daily = resample_to_daily(gw_raw, agg="mean")
    if min_daily_std > 0.0 and float(gw_daily["value"].std()) < min_daily_std:
        return None

    rain_series = []
    for k in ["RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3"]:
        mid = row.get(k)
        if pd.notna(mid):
            raw = load_timeseries(str(mid), "rainfall", str(raw_root))
            rain_series.append(resample_to_daily(raw, agg="sum") if raw is not None else None)
        else:
            rain_series.append(None)
    rainfall_daily = average_rainfall(rain_series)
    return join_timeseries(gw_daily, rainfall_daily)


def _n_gap_days(joined: pd.DataFrame) -> int:
    """Missing calendar days within [joined.index.min(), joined.index.max()]."""
    if joined.empty:
        return 0
    span_days = int((joined.index.max() - joined.index.min()).days) + 1
    return span_days - len(joined)


_FEATURE_COLS = [
    "GW_Lag1", "GW_Lag7", "GW_Lag30",
    "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum",
    "day_of_year", "Sin_DOY", "Cos_DOY",
    "Recharge_Weibull",
    "Recharge_East_masked", "Recharge_West_masked", "region_group_code",
]


def run_fleet_comparison(config: dict) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    raw_root = ROOT / config["download"]["raw_root"]
    links_path = ROOT / config["linking"]["output_path"]
    weibull_cfg = config["features"].get("weibull")
    weibull_multi_cfg = config["features"].get("weibull_multi")
    weibull_by_group_cfg = config["features"].get("weibull_by_group")
    cleaning_cfg = config["features"].get("cleaning", {})
    iqr_fence = float(cleaning_cfg.get("groundwater_iqr_fence", 20.0))
    min_daily_std = float(cleaning_cfg.get("groundwater_min_daily_std", 0.0))
    lon_split = float(config["regional"]["lon_split"])

    gw_lons_map: dict[str, float] = {}
    needs_groups = (
        (weibull_multi_cfg and weibull_multi_cfg.get("enabled"))
        or (weibull_by_group_cfg and weibull_by_group_cfg.get("enabled"))
    )
    if needs_groups:
        cat_path = ROOT / config["catalogue"]["output_path"]
        cat = pd.read_csv(cat_path)
        gw_cat = (cat[cat["measure_type"] == "groundwater"][["station_id", "lon"]]
                  .drop_duplicates("station_id").set_index("station_id")["lon"])
        gw_lons_map = gw_cat.to_dict()

    links = pd.read_csv(links_path)
    print(f"Processing {len(links)} GW stations for the A/B comparison...")

    old_by_station: dict[str, pd.DataFrame] = {}
    new_by_station: dict[str, pd.DataFrame] = {}
    records = []

    for _, row in links.iterrows():
        sid = str(row["GWStationID"])
        joined = _build_joined(row, raw_root, iqr_fence, min_daily_std)
        if joined is None or joined.empty:
            continue

        region_group = get_station_group(sid, gw_lons_map, lon_split)
        old_feat = OLD_create_features(
            joined, weibull_cfg=weibull_cfg, weibull_multi_cfg=weibull_multi_cfg,
            weibull_by_group_cfg=weibull_by_group_cfg, region_group=region_group)
        new_feat = NEW_create_features(
            joined, weibull_cfg=weibull_cfg, weibull_multi_cfg=weibull_multi_cfg,
            weibull_by_group_cfg=weibull_by_group_cfg, region_group=region_group)

        old_by_station[sid] = old_feat
        new_by_station[sid] = new_feat

        n_gap = _n_gap_days(joined)
        shared_idx = old_feat.index.intersection(new_feat.index)

        rec = {
            "station_id": sid,
            "rows_before": len(joined),
            "n_gap_days": n_gap,
            "rows_after_old": len(old_feat),
            "rows_after_new": len(new_feat),
            "shared_rows": len(shared_idx),
        }

        max_abs_total = 0.0
        mean_abs_total = 0.0
        n_cols_compared = 0
        for col in _FEATURE_COLS:
            if col not in old_feat.columns or col not in new_feat.columns:
                continue
            if len(shared_idx) == 0:
                rec[f"{col}__max_abs_diff"] = np.nan
                rec[f"{col}__mean_abs_diff"] = np.nan
                continue
            a = old_feat.loc[shared_idx, col].to_numpy(dtype=float)
            b = new_feat.loc[shared_idx, col].to_numpy(dtype=float)
            diff = np.abs(a - b)
            max_d = float(np.nanmax(diff)) if len(diff) else np.nan
            mean_d = float(np.nanmean(diff)) if len(diff) else np.nan
            rec[f"{col}__max_abs_diff"] = max_d
            rec[f"{col}__mean_abs_diff"] = mean_d
            if np.isfinite(max_d):
                max_abs_total = max(max_abs_total, max_d)
            if np.isfinite(mean_d):
                mean_abs_total += mean_d
                n_cols_compared += 1

        rec["max_abs_diff_any_col"] = max_abs_total
        rec["mean_abs_diff_sum"] = mean_abs_total
        records.append(rec)
        print(f"  [{sid[:8]}] before={rec['rows_before']:>5} gaps={n_gap:>4} "
              f"old={rec['rows_after_old']:>5} new={rec['rows_after_new']:>5} "
              f"maxdiff={max_abs_total:.4g}")

    detail = pd.DataFrame(records)
    return detail, old_by_station, new_by_station


# ---------------------------------------------------------------------------
# Roll (reduced-form) cross-check re-run, reusing cached forecast members --
# no network fetch.
# ---------------------------------------------------------------------------

def run_roll_comparison(config: dict, old_by_station: dict[str, pd.DataFrame],
                        new_by_station: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame | None, str | None]:
    members_path = ROOT / "data" / "model" / "forecast_ensemble_members.parquet"
    if not members_path.exists():
        return None, ("No cached data/model/forecast_ensemble_members.parquet found -- "
                       "the roll cross-check needs the provider's per-member forecast "
                       "rainfall (precip_mm/recharge_weibull per member/date), which is "
                       "only produced by scripts.build_ensemble_members hitting the live "
                       "ECMWF/Open-Meteo forecast APIs. Run that first, or diff roll_p50 "
                       "on the next production forecast rebuild instead.")

    from src.forecast.ensemble import gw_roll
    from src.forecast.ensemble.members import _seed_gw_dgw
    from src.forecast.ensemble.seeding import freshest_gw

    method = config["forecast"]["ensemble"].get("gw_roll_method", "reduced_form_ar")
    cached = pd.read_parquet(members_path)
    cached["date"] = pd.to_datetime(cached["date"])

    rows = []
    for sid, grp in cached.groupby("station_id"):
        hist_old = old_by_station.get(sid)
        hist_new = new_by_station.get(sid)
        if hist_old is None or hist_new is None or hist_old.empty or hist_new.empty:
            continue
        try:
            params_old = gw_roll.fit(method, hist_old)
            params_new = gw_roll.fit(method, hist_new)
        except Exception as exc:
            print(f"  ! {sid[:8]}: roll fit failed ({exc}) -- skipped")
            continue

        def _widened_clip(hist, fresh_pair):
            dgw_clip, gw_clip = gw_roll.station_guardrails(hist)
            seed_gw, seed_dgw = fresh_pair
            if seed_gw is not None and np.isfinite(seed_gw):
                margin = 0.1 * (gw_clip[1] - gw_clip[0])
                gw_clip = (min(gw_clip[0], seed_gw - margin), max(gw_clip[1], seed_gw + margin))
            return dgw_clip, gw_clip

        fresh = freshest_gw(sid)
        seed_old = _seed_gw_dgw(hist_old, fresh)
        seed_new = _seed_gw_dgw(hist_new, fresh)
        dgw_clip_old, gw_clip_old = _widened_clip(hist_old, seed_old)
        dgw_clip_new, gw_clip_new = _widened_clip(hist_new, seed_new)

        forecast_dates = pd.DatetimeIndex(sorted(grp["date"].unique()))
        doy = forecast_dates.day_of_year.values
        exog_base = pd.DataFrame({
            "Sin_DOY": np.sin(2 * np.pi * doy / 365.25),
            "Cos_DOY": np.cos(2 * np.pi * doy / 365.25),
        }, index=forecast_dates)

        preds_old, preds_new = [], []
        for m, g in grp.groupby("member"):
            g = g.set_index("date").reindex(forecast_dates)
            rech = g["recharge_weibull"]
            # recharge_weibull in the cache is forecast-side (bridged, always
            # zero-filled before the convolution -- unaffected by this fix);
            # reuse it as-is so this comparison isolates the roll-fit change.
            if rech.isna().any():
                continue
            exog = exog_base.copy()
            exog["Recharge_Weibull"] = rech.to_numpy(dtype=float)
            try:
                gw_old = gw_roll.roll(method, seed_gw=seed_old[0], seed_dgw=seed_old[1],
                                      exog_future=exog, params=params_old,
                                      dgw_clip=dgw_clip_old, gw_clip=gw_clip_old)
                gw_new = gw_roll.roll(method, seed_gw=seed_new[0], seed_dgw=seed_new[1],
                                      exog_future=exog, params=params_new,
                                      dgw_clip=dgw_clip_new, gw_clip=gw_clip_new)
            except Exception:
                continue
            preds_old.append(gw_old)
            preds_new.append(gw_new)

        if not preds_old:
            continue
        roll_p50_old = np.median(np.vstack(preds_old), axis=0)
        roll_p50_new = np.median(np.vstack(preds_new), axis=0)
        diff = roll_p50_new - roll_p50_old
        rows.append({
            "station_id": sid,
            "n_forecast_dates": len(diff),
            "n_members": len(preds_old),
            "mean_abs_diff_m": float(np.mean(np.abs(diff))),
            "max_abs_diff_m": float(np.max(np.abs(diff))),
            "mean_signed_diff_m": float(np.mean(diff)),
        })

    if not rows:
        return None, "Roll re-fit produced no comparable stations (fit/roll errors for all)."
    return pd.DataFrame(rows), None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(x: float, nd: int = 4) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:.{nd}g}"


def write_report(detail: pd.DataFrame, roll_diff: pd.DataFrame | None,
                 roll_skip_reason: str | None) -> str:
    out_dir = ROOT / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    detail.to_csv(out_dir / "ab_gap_features_detail.csv", index=False)
    if roll_diff is not None:
        roll_diff.to_csv(out_dir / "ab_gap_features_roll.csv", index=False)

    n_stations = len(detail)
    zero_gap = detail[detail["n_gap_days"] == 0]
    gappy = detail[detail["n_gap_days"] > 0]

    equiv_fail = zero_gap[zero_gap["max_abs_diff_any_col"] > _EQUIV_ATOL]
    equiv_pass = len(zero_gap) - len(equiv_fail)

    movers = detail.sort_values("mean_abs_diff_sum", ascending=False)
    top20 = movers.head(20)
    corr = (detail[["mean_abs_diff_sum", "n_gap_days"]]
            .corr(method="spearman").iloc[0, 1] if n_stations >= 3 else float("nan"))
    top20_n_gap_days_gt0 = int((top20["n_gap_days"] > 0).sum())

    total_rows_before = int(detail["rows_before"].sum())
    total_rows_old = int(detail["rows_after_old"].sum())
    total_rows_new = int(detail["rows_after_new"].sum())

    lines = []
    lines.append("# A/B report: calendar-true gap-features fix")
    lines.append("")
    lines.append("BUGS.md — \"Lag / rolling / Weibull features computed positionally over a "
                "gap-collapsed daily index, mislabelling temporal distance\". Compares the "
                "OLD (positional, pre-fix) and NEW (calendar-reindexed) feature computation "
                "on the real local fleet (`data/raw` + `data/processed/station_links.csv`).")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Stations compared: **{n_stations}** "
                f"({len(zero_gap)} gap-free, {len(gappy)} with at least one gap day).")
    lines.append(f"- Total feature rows: old={total_rows_old}, new={total_rows_new} "
                f"(Δ={total_rows_new - total_rows_old}, of {total_rows_before} joined rows before feature engineering).")
    lines.append(f"- **Equivalence guarantee (gap-free stations)**: {equiv_pass}/{len(zero_gap)} "
                f"stations have zero diff (atol={_EQUIV_ATOL}) on shared rows across every "
                f"feature column. "
                + ("PASS." if len(equiv_fail) == 0 else
                   f"**FAIL — {len(equiv_fail)} gap-free station(s) diverged:** "
                   + ", ".join(equiv_fail['station_id'].str[:8].tolist())))
    lines.append(f"- Top-20 movers by total mean-abs-diff: {top20_n_gap_days_gt0}/20 have at "
                f"least one gap day.")
    lines.append(f"- Spearman correlation(mover magnitude, n_gap_days) across all "
                f"{n_stations} stations: **{_fmt(corr, 3)}**.")
    if roll_diff is not None:
        lines.append(f"- roll_p50 re-fit diff computed for **{len(roll_diff)}** stations "
                    f"(reusing cached `forecast_ensemble_members.parquet`, no external fetch). "
                    f"Mean |Δ| across stations: {_fmt(roll_diff['mean_abs_diff_m'].mean())} m, "
                    f"max |Δ|: {_fmt(roll_diff['max_abs_diff_m'].max())} m.")
    else:
        lines.append(f"- roll_p50 diff: **not run** — {roll_skip_reason}")
    lines.append("")

    lines.append("## Zero-gap equivalence detail")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---:|")
    lines.append(f"| gap-free stations | {len(zero_gap)} |")
    lines.append(f"| passing (diff <= {_EQUIV_ATOL}) | {equiv_pass} |")
    lines.append(f"| failing | {len(equiv_fail)} |")
    lines.append("")

    lines.append("## Top-20 movers (by summed mean-abs-diff across feature columns)")
    lines.append("")
    lines.append("| station_id | n_gap_days | rows_before | rows_old | rows_new | "
                "max_abs_diff | mean_abs_diff_sum |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, r in top20.iterrows():
        lines.append(
            f"| {r['station_id'][:8]} | {int(r['n_gap_days'])} | {int(r['rows_before'])} | "
            f"{int(r['rows_after_old'])} | {int(r['rows_after_new'])} | "
            f"{_fmt(r['max_abs_diff_any_col'])} | {_fmt(r['mean_abs_diff_sum'])} |")
    lines.append("")

    if roll_diff is not None:
        lines.append("## roll_p50 diff distribution (new vs old fit, shared forecast members)")
        lines.append("")
        lines.append("| statistic | value (m) |")
        lines.append("|---|---:|")
        lines.append(f"| mean of per-station mean\\|Δ\\| | {_fmt(roll_diff['mean_abs_diff_m'].mean())} |")
        lines.append(f"| median of per-station mean\\|Δ\\| | {_fmt(roll_diff['mean_abs_diff_m'].median())} |")
        lines.append(f"| max of per-station max\\|Δ\\| | {_fmt(roll_diff['max_abs_diff_m'].max())} |")
        lines.append(f"| p90 of per-station mean\\|Δ\\| | {_fmt(roll_diff['mean_abs_diff_m'].quantile(0.9))} |")
        lines.append("")
        top_roll = roll_diff.sort_values("mean_abs_diff_m", ascending=False).head(10)
        lines.append("Top 10 stations by mean roll_p50 |Δ|:")
        lines.append("")
        lines.append("| station_id | n_forecast_dates | n_members | mean\\|Δ\\| (m) | max\\|Δ\\| (m) |")
        lines.append("|---|---:|---:|---:|---:|")
        for _, r in top_roll.iterrows():
            lines.append(f"| {r['station_id'][:8]} | {int(r['n_forecast_dates'])} | "
                        f"{int(r['n_members'])} | {_fmt(r['mean_abs_diff_m'])} | {_fmt(r['max_abs_diff_m'])} |")
        lines.append("")
    else:
        lines.append("## roll_p50 diff")
        lines.append("")
        lines.append(f"Not run: {roll_skip_reason}")
        lines.append("")
        lines.append("Production rebuild will need: `python -m scripts.run_chain --core "
                    "--forecast --publish` (features rebuild via `--core`, then a fresh "
                    "`build_ensemble_members` run against the live provider to regenerate "
                    "`forecast_ensemble_members.parquet` on the fixed features, then "
                    "`build_pastas_summary` to recompute `roll_p50`/`model_spread`).")
        lines.append("")

    lines.append("## Full per-station detail")
    lines.append("")
    lines.append("See `outputs/ab_gap_features_detail.csv` "
                f"({len(detail)} rows) for the complete per-station, per-column table"
                + (" and `outputs/ab_gap_features_roll.csv` for the roll diff." if roll_diff is not None else "."))
    lines.append("")

    report_text = "\n".join(lines)
    (out_dir / "ab_gap_features.md").write_text(report_text, encoding="utf-8")

    if len(equiv_fail):
        raise SystemExit(
            f"EQUIVALENCE GUARANTEE FAILED: {len(equiv_fail)} gap-free station(s) diverged "
            f"between old and new feature computation -- see outputs/ab_gap_features.md")

    return report_text


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--roll", action="store_true",
                        help="Also diff roll_p50 (reuses cached forecast members, no fetch).")
    args = parser.parse_args()

    config = _load_config()
    detail, old_by_station, new_by_station = run_fleet_comparison(config)

    roll_diff, roll_skip_reason = (None, "pass --roll to compute (skipped by default)")
    if args.roll:
        roll_diff, roll_skip_reason = run_roll_comparison(config, old_by_station, new_by_station)

    report = write_report(detail, roll_diff, roll_skip_reason)
    print("\n" + report)
    print(f"\nWrote outputs/ab_gap_features.md, outputs/ab_gap_features_detail.csv"
          + (", outputs/ab_gap_features_roll.csv" if roll_diff is not None else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
