"""Probabilistic verification of the GW-roll hindcast -> outputs/ (Phase 3 / A).

Report-only. Scores the operational roll method's predictive band (point forecast
+ train-estimated per-lead error spread) on out-of-sample TEST origins: CRPS, PIT
calibration, spread-skill, and CRPS-skill vs persistence + climatology baselines.

    python -m scripts.build_hindcast_verification [--method M] [--stations N]

HONEST SCOPE: this is the roll band under perfect-forecast — a partial surface,
NOT the headline Pastas-ensemble fan (which is winter-gated on the 0.1 archive).
See docs/phase3_verification_scope.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.utils.io_encoding import force_utf8_stdio
from src.diagnostics.hindcast_prob import run_prob_hindcast

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    force_utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--method", default="reduced_form_ar",
                    choices=["reduced_form", "reduced_form_ar", "reduced_form_cr"])
    ap.add_argument("--stations", type=int, default=12)
    ap.add_argument("--horizon", type=int, default=14)
    args = ap.parse_args(argv)

    cfg = json.loads((ROOT / "config/config.json").read_text(encoding="utf-8"))
    res = run_prob_hindcast(cfg, method=args.method, n_stations=args.stations,
                            horizon=args.horizon)
    summary, pit = res["summary"], res["pit"]
    if summary.empty:
        print("no test origins scored — nothing to write.")
        return 0

    out_csv = ROOT / "outputs" / "hindcast_verification.csv"
    out_md = ROOT / "outputs" / "hindcast_verification.md"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out_csv, index=False)

    # headline lead-7 / lead-horizon CRPSS for the console
    def _at(lead, col):
        r = summary[summary["lead"] == lead]
        return float(r[col].iloc[0]) if not r.empty else float("nan")
    print(f"prob-hindcast ({res['method']}, {res['n_pilots']} pilots, "
          f"{res['n_rows']} scored leads):")
    print(f"  CRPSS vs persistence  lead-7 {_at(7,'crpss_persist'):+.3f} | "
          f"lead-{args.horizon} {_at(args.horizon,'crpss_persist'):+.3f}")
    print(f"  CRPSS vs climatology  lead-7 {_at(7,'crpss_clim'):+.3f} | "
          f"lead-{args.horizon} {_at(args.horizon,'crpss_clim'):+.3f}")
    print(f"  spread/RMSE (lead-7)  {_at(7,'spread_skill'):.2f} "
          f"(<1 over-confident, >1 over-dispersed)")

    lines = [
        "# GW-roll probabilistic hindcast verification (Phase 3 / A)",
        "",
        f"Method **{res['method']}**, {res['n_pilots']} pilot stations, "
        f"horizon {args.horizon} d, {res['n_rows']} scored (origin, lead) pairs.",
        "",
        "> **Scope:** the roll point forecast wrapped in its train-estimated "
        "per-lead error spread, scored on out-of-sample TEST origins under "
        "perfect-forecast. A **partial** surface — *not* the headline "
        "Pastas-ensemble fan (winter-gated), and weather error is excluded. "
        "Leakage-safe: band + baselines fit on train, only test origins scored.",
        "",
        "## Per-lead scores",
        "",
        "CRPSS > 0 = beats that baseline. spread/RMSE ~1 = well-dispersed "
        "(<1 over-confident).",
        "",
        "| lead | n | mean CRPS | CRPSS vs persist | CRPSS vs clim | spread/RMSE | PIT mean |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in summary.iterrows():
        lines.append(
            f"| {int(r['lead'])} | {int(r['n'])} | {r['mean_crps']:.3f} | "
            f"{r['crpss_persist']:+.3f} | {r['crpss_clim']:+.3f} | "
            f"{r['spread_skill']:.2f} | {r['pit_mean']:.2f} |")
    lines += ["", "## PIT calibration (uniform = calibrated)", "",
              "| bin | frac | dev |", "|---|---:|---:|"]
    for _, r in pit.iterrows():
        lines.append(f"| {r['bin_lo']:.1f}–{r['bin_hi']:.1f} | "
                     f"{r['frac']:.3f} | {r['dev']:+.3f} |")
    miscal = float(pit["dev"].abs().sum()) if not pit.empty else float("nan")
    lines += ["", f"PIT |dev| sum = {miscal:.3f} (0 = perfectly flat).", ""]
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"report -> {out_md.relative_to(ROOT)} (+ .csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
