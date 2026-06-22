"""W4 live validation — CDS ERA5 daily precip, sanity-checked against the
cached Open-Meteo `era5_precip` series.

Important framing (established 2026-06-13): the two are NOT the same
product. CDS serves **raw ERA5** (0.25 deg); Open-Meteo's archive serves a
**downscaled ERA5/ERA5-Land blend** (~9 km), which runs systematically
higher on heavy frontal/orographic days over the chalk. Daily r is ~0.75
(annual) / ~0.88 (winter) and annual totals differ ~6-14% — a real source
difference, not an extraction error. So this script does NOT test for
equality; it tests that our extraction is CORRECT and the magnitudes are
sane:

  1. **Day alignment** (the only thing that would be OUR bug): the
     zero-shift correlation must beat its +-1-day neighbours by a wide
     margin — a de-accumulation / labelling error craters this.
  2. **Magnitude**: annual totals and mean bias within source-difference
     tolerance.

Consequence for cutover: because CDS-ERA5 != OM-ERA5, the per-borehole
bias factor f_bh (mean-gauge / mean-ERA5) must be REFIT against CDS-ERA5
(a cheap per-borehole mean ratio, folded into the `--pastas`
recalibration) rather than carried over.

Needs a CDS key + the `derived-era5-single-levels-daily-statistics`
licence accepted.

    python -m scripts.validate_cds_era5 [--stations 3] [--start 2022-01-01]
        [--end 2022-12-31]

Exit 0 = every station extracts correctly (zero-shift r beats neighbours
by >= 0.3 and is >= 0.70; |annual-total rel| <= 0.20; |mean bias| <= 0.4
mm/day); 1 otherwise. Report: outputs/cds_era5_validation.md.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.cds_era5 import (  # noqa: E402
    PRECIP_VAR, fetch_daily_box, point_series, precip_mm_from_daily_mean)
from src.data.era5_precip import PRECIP_CACHE_ROOT  # noqa: E402

# Extraction-correctness gates (NOT equality — see the module docstring).
ALIGN_MARGIN = 0.30        # zero-shift r must beat +-1-day r by this
ALIGN_R_MIN = 0.70         # ...and clear this absolute floor
BIAS_MAX = 0.40            # mm/day mean bias (source-difference tolerance)
TOTAL_REL_MAX = 0.20       # annual-total relative difference


def _iso(s: str) -> date:
    return pd.Timestamp(s).date()


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", type=int, default=3)
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2022-12-31")
    args = ap.parse_args(argv)
    start, end = _iso(args.start), _iso(args.end)

    cat = pd.read_csv(ROOT / "data/processed/catalogue.csv")
    cat = (cat[cat["measure_type"] == "groundwater"]
           .drop_duplicates("station_id").set_index("station_id"))
    cached = sorted(PRECIP_CACHE_ROOT.glob("*.csv"))
    picks = [p for p in cached if p.stem in cat.index][:args.stations]
    if not picks:
        print("no ERA5-precip-cached boreholes found")
        return 1

    # One CDS box fetch serves every borehole point.
    print(f"fetching CDS precip box {start} -> {end} ...")
    ds = fetch_daily_box(*PRECIP_VAR, start, end)

    rows = []
    for p in picks:
        sid = p.stem
        lat, lon = float(cat.loc[sid, "lat"]), float(cat.loc[sid, "lon"])
        name = str(cat.loc[sid, "station_name"])
        cds = precip_mm_from_daily_mean(point_series(ds, lat, lon))
        ref = pd.read_csv(p, parse_dates=["date"]).set_index("date")["precip_mm"]
        ref.index = pd.DatetimeIndex(ref.index).tz_localize(None)
        m = pd.concat([cds.rename("cds"), ref.rename("om")], axis=1).dropna()
        if len(m) < 200:
            print(f"  {name}: only {len(m)} overlapping days - skipping")
            continue

        def _r(shift):
            mm = pd.concat([cds.shift(shift).rename("c"), ref.rename("o")],
                           axis=1).dropna()
            return float(np.corrcoef(mm["c"], mm["o"])[0, 1])

        r0, rm1, rp1 = _r(0), _r(-1), _r(1)
        aligned = (r0 >= ALIGN_R_MIN
                   and r0 - max(rm1, rp1) >= ALIGN_MARGIN)
        bias = float((m["cds"] - m["om"]).mean())
        tot_rel = float(abs(m["cds"].sum() - m["om"].sum()) / m["om"].sum())
        ok = aligned and abs(bias) <= BIAS_MAX and tot_rel <= TOTAL_REL_MAX
        rows.append({"station": name, "n_days": len(m), "r0": r0,
                     "r_neighbour": max(rm1, rp1), "bias": bias,
                     "annual_total_rel": tot_rel, "aligned": aligned,
                     "pass": ok})
        print(f"  {name[:30]:<30} n={len(m)} r0={r0:.3f} "
              f"(+-1d {max(rm1, rp1):.3f}) bias={bias:+.3f} "
              f"tot_rel={tot_rel:.1%} "
              f"{'PASS' if ok else 'FAIL'}")

    if not rows:
        return 1
    res = pd.DataFrame(rows)
    verdict = "PASS" if bool(res["pass"].all()) else "FAIL"

    out = ROOT / "outputs" / "cds_era5_validation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# CDS ERA5 precip validation vs cached Open-Meteo `era5_precip` (W4)",
        "",
        f"**Verdict: {verdict}**",
        "",
        f"- Generated: {pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- Overlap window: {start} -> {end} (daily)",
        "- **This is an extraction-correctness check, not an equality "
        "check.** CDS serves raw ERA5 (0.25 deg); Open-Meteo's archive "
        "serves a downscaled ERA5/ERA5-Land blend (~9 km) that runs higher "
        "on heavy orographic days. Daily r ~0.75-0.88 and ~6-14% total "
        "difference are the real source gap. The zero-shift correlation "
        "decisively beating its +-1-day neighbours is what proves our "
        "de-accumulation + day labelling are correct.",
        "",
        "| Borehole | days | r (0-shift) | r (+-1d) | mean bias (mm/d) | "
        "annual-total rel | aligned | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, r_ in res.iterrows():
        lines.append(f"| {r_['station']} | {int(r_['n_days'])} | "
                     f"{r_['r0']:.3f} | {r_['r_neighbour']:.3f} | "
                     f"{r_['bias']:+.3f} | {r_['annual_total_rel']:.1%} | "
                     f"{'OK' if r_['aligned'] else 'FAIL'} | "
                     f"{'PASS' if r_['pass'] else 'FAIL'} |")
    lines += [
        "",
        f"Gates: zero-shift r >= {ALIGN_R_MIN} and beats the better +-1-day "
        f"neighbour by >= {ALIGN_MARGIN} (correct day alignment); |mean "
        f"bias| <= {BIAS_MAX} mm/day; |annual-total rel| <= {TOTAL_REL_MAX:.0%}.",
        "",
        "**Cutover consequence**: because CDS-ERA5 != OM-ERA5, the "
        "per-borehole bias factor `f_bh` (mean-gauge / mean-ERA5) must be "
        "refit against CDS-ERA5 at cutover (a per-borehole mean ratio, "
        "folded into `run_chain --pastas`) rather than carried over.",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"verdict: {verdict} -> {out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
