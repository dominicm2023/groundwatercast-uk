"""
Cross-reference the rainfall stations we actually use with the EA
flood-monitoring API's rainfall gauges.

Why
---
Historical rainfall comes from the EA *hydrology* archive (lags weeks to
months).  The flood-monitoring API is the EA's near-real-time feed for
the same tipping-bucket gauges.  Gauges that appear in both can have
their daily-total record extended right up to "now", closing the 0-2 day
staleness in ``Recharge_Weibull`` (see ``scripts.v19_refresh_live_rainfall``).

This mirrors ``src.diagnostics.flood_monitoring_xref`` (which does the same
for groundwater) and reuses its match helpers.  The crucial scoping
difference: we only cross-reference the rainfall stations that are
*actually wired into a borehole* — i.e. the unique ``RainMeasureID_1/2/3``
values in ``station_links.csv`` — not every rain gauge nationwide.

Match strategy (in order, first hit wins) — identical ladder to the GW
xref: reference -> coords (<=50 m) -> name_exact -> name_fuzzy -> none.

Output
------
- ``data/processed/rainfall_monitoring_xref.csv`` — one row per used
  rainfall station, keyed by ``rain_measure_id`` (the raw-CSV filename
  stem) with the matched ``fm_notation``.
- ``outputs/rainfall_monitoring_coverage_report.csv`` — freshness sample.

Run with:
    python -m src.diagnostics.rainfall_monitoring_xref
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

from src.utils.io_encoding import force_utf8_stdio
from src.diagnostics.flood_monitoring_xref import (
    _FM_BASE,
    _LIST_URL,
    _COORD_TOLERANCE_M,
    _FUZZY_THRESHOLD,
    _REQUEST_TIMEOUT_S,
    _haversine_m,
    _norm_name,
    _fuzzy_ratio,
    _refs_for_hydro_station,
    fetch_hydrology_reference_index,
)

ROOT = Path(__file__).parents[2]

_GUID_LEN = 36  # standard UUID length; the leading slug segment of a measure_id


# ---------------------------------------------------------------------------
# Flood-monitoring rainfall fetcher
# ---------------------------------------------------------------------------

def fetch_flood_monitoring_rain_stations() -> pd.DataFrame:
    """
    Fetch every rainfall station from the flood-monitoring API.

    Returns one row per station with the reference fields we match on.
    Some rows may have null lat/lon because the listing endpoint doesn't
    carry coordinates for every station.
    """
    r = requests.get(
        _LIST_URL,
        params={"parameter": "rainfall", "_limit": 20_000},
        timeout=_REQUEST_TIMEOUT_S,
    )
    r.raise_for_status()
    items = r.json().get("items", [])

    rows: list[dict] = []
    for it in items:
        notation = it.get("notation")
        label = it.get("label")
        ref = it.get("stationReference") or notation
        rows.append({
            "fm_notation":          notation,
            "fm_label":             label,
            "fm_station_reference": ref,
            "fm_lat":               it.get("lat"),
            "fm_lon":               it.get("long"),
            "fm_status":            it.get("status"),
            "fm_date_opened":       it.get("dateOpened"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Which rainfall stations do we actually use?
# ---------------------------------------------------------------------------

def used_rain_stations(
    links_path: Path | None = None,
    catalogue_path: Path | None = None,
) -> pd.DataFrame:
    """
    Collect the unique rainfall stations wired into at least one borehole.

    Reads the ``RainMeasureID_1/2/3`` columns from ``station_links.csv``,
    deduplicates, and enriches each with the catalogue's station name and
    coordinates (joined on the 36-char GUID prefix of the measure_id).

    Returns columns:
        rain_measure_id, station_id, station_name, lat, lon
    """
    links_path = links_path or (ROOT / "data" / "processed" / "station_links.csv")
    catalogue_path = catalogue_path or (ROOT / "data" / "processed" / "catalogue.csv")

    links = pd.read_csv(links_path)
    rain_ids = pd.unique(
        pd.concat(
            [links[c] for c in ("RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3")
             if c in links.columns],
            ignore_index=True,
        ).dropna()
    )
    df = pd.DataFrame({"rain_measure_id": rain_ids})
    df["station_id"] = df["rain_measure_id"].str.slice(0, _GUID_LEN)

    cat = pd.read_csv(catalogue_path)
    meta = (
        cat[cat["measure_type"] == "rainfall"]
        [["station_id", "station_name", "lat", "lon"]]
        .drop_duplicates("station_id")
    )
    df = df.merge(meta, on="station_id", how="left")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Cross-reference builder
# ---------------------------------------------------------------------------

def build_rainfall_xref(
    links_path: Path | None = None,
    catalogue_path: Path | None = None,
    fm_stations: pd.DataFrame | None = None,
    hydro_index: dict[str, dict] | None = None,
) -> pd.DataFrame:
    """
    Build the cross-reference between used rainfall stations and
    flood-monitoring rainfall gauges.

    Returns one row per used rainfall station with a ``match_method``
    column (``reference``, ``coords``, ``name_exact``, ``name_fuzzy``,
    ``none``).
    """
    used = used_rain_stations(links_path, catalogue_path)

    if fm_stations is None:
        fm_stations = fetch_flood_monitoring_rain_stations()
    if hydro_index is None:
        hydro_index = fetch_hydrology_reference_index()

    used["_norm_name"] = used["station_name"].map(_norm_name)
    fm_stations = fm_stations.copy()
    fm_stations["_norm_label"] = fm_stations["fm_label"].map(_norm_name)

    # FM reference index for O(1) lookup by any reference field
    fm_by_ref: dict[str, pd.Series] = {}
    for _, fm_row in fm_stations.iterrows():
        for key in ("fm_notation", "fm_station_reference"):
            v = fm_row.get(key)
            if v and not isinstance(v, (list, dict)):
                fm_by_ref.setdefault(str(v).strip().upper(), fm_row)

    fm_with_coords = fm_stations.dropna(subset=["fm_lat", "fm_lon"]).copy()

    results: list[dict] = []
    for _, row in used.iterrows():
        sid = row["station_id"]
        sname = row["station_name"]
        slat, slon = row["lat"], row["lon"]
        snorm = row["_norm_name"]

        match: dict | None = None

        # 1. Reference match via the hydrology listing (stationReference /
        # wiskiID / notation) — strongest signal when present.
        h = hydro_index.get(str(sid))
        if h:
            for ref in _refs_for_hydro_station(h):
                fm_hit = fm_by_ref.get(ref)
                if fm_hit is not None:
                    match = {
                        "match_method": "reference",
                        "match_distance_m": 0.0,
                        "match_confidence": "high",
                        "fm_notation": fm_hit["fm_notation"],
                        "fm_label": fm_hit["fm_label"],
                        "matched_ref": ref,
                    }
                    break

        # 2. Coordinate match (50 m) — only if both have coords
        if match is None and pd.notna(slat) and pd.notna(slon) and not fm_with_coords.empty:
            dists = fm_with_coords.apply(
                lambda r: _haversine_m(slat, slon, float(r["fm_lat"]), float(r["fm_lon"])),
                axis=1,
            )
            if not dists.empty:
                idx_min = dists.idxmin()
                min_d = float(dists.loc[idx_min])
                if min_d <= _COORD_TOLERANCE_M:
                    hit = fm_with_coords.loc[idx_min]
                    match = {
                        "match_method": "coords",
                        "match_distance_m": round(min_d, 1),
                        "match_confidence": "high" if min_d <= 25 else "medium",
                        "fm_notation": hit["fm_notation"],
                        "fm_label": hit["fm_label"],
                    }

        # 3. Exact name match (normalised)
        if match is None and snorm:
            hit = fm_stations[fm_stations["_norm_label"] == snorm]
            if not hit.empty:
                match = {
                    "match_method": "name_exact",
                    "match_distance_m": None,
                    "match_confidence": "high",
                    "fm_notation": hit.iloc[0]["fm_notation"],
                    "fm_label": hit.iloc[0]["fm_label"],
                }

        # 4. Fuzzy name match
        if match is None and snorm:
            best_score = 0
            best_idx = None
            for idx, fm_norm in fm_stations["_norm_label"].items():
                if not fm_norm:
                    continue
                score = _fuzzy_ratio(snorm, fm_norm)
                if score > best_score:
                    best_score = score
                    best_idx = idx
            if best_score >= _FUZZY_THRESHOLD and best_idx is not None:
                hit = fm_stations.loc[best_idx]
                match = {
                    "match_method": "name_fuzzy",
                    "match_distance_m": None,
                    "match_confidence": "low",  # always flag for review
                    "fm_notation": hit["fm_notation"],
                    "fm_label": hit["fm_label"],
                    "fuzzy_score": best_score,
                }

        if match is None:
            match = {
                "match_method": "none",
                "match_distance_m": None,
                "match_confidence": None,
                "fm_notation": None,
                "fm_label": None,
            }

        results.append({
            "rain_measure_id": row["rain_measure_id"],
            "station_id":      sid,
            "station_name":    sname,
            "hydro_lat":       slat,
            "hydro_lon":       slon,
            **match,
        })

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Coverage report (freshness sampling)
# ---------------------------------------------------------------------------

def measure_freshness(xref: pd.DataFrame, sample_n: int = 20) -> pd.DataFrame:
    """
    For a sample of matched stations, fetch the latest flood-monitoring
    rainfall reading and compute its age.  Returns the matched DataFrame
    with ``latest_reading`` and ``latest_age_minutes`` columns.
    """
    matched = xref[xref["fm_notation"].notna()].copy()
    if matched.empty:
        matched["latest_reading"] = pd.NaT
        matched["latest_age_minutes"] = pd.NA
        return matched

    sample = matched.sample(min(sample_n, len(matched)), random_state=0)
    now = pd.Timestamp.now(tz="UTC")
    latest: dict[str, pd.Timestamp | None] = {}
    for _, row in sample.iterrows():
        notation = row["fm_notation"]
        try:
            r = requests.get(
                f"{_FM_BASE}/id/stations/{notation}/readings",
                params={"_sorted": "", "_limit": 1},
                timeout=15,
            )
            items = r.json().get("items", [])
            latest[notation] = (
                pd.Timestamp(items[0]["dateTime"]).tz_convert("UTC") if items else None
            )
        except Exception:
            latest[notation] = None
        time.sleep(0.05)

    matched["latest_reading"] = matched["fm_notation"].map(latest)
    matched["latest_age_minutes"] = (
        (now - matched["latest_reading"]).dt.total_seconds() / 60.0
    ).round(0)
    return matched


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> int:
    force_utf8_stdio()
    print("Fetching flood-monitoring rainfall stations...")
    fm = fetch_flood_monitoring_rain_stations()
    print(f"  {len(fm)} flood-monitoring rainfall stations nationwide")
    print(f"  {fm['fm_lat'].notna().sum()} have coordinates in the listing")

    print("\nFetching hydrology API reference index...")
    hydro_idx = fetch_hydrology_reference_index()
    print(f"  {len(hydro_idx)} hydrology stations indexed by GUID")

    print("\nBuilding rainfall cross-reference (used stations only)...")
    xref = build_rainfall_xref(fm_stations=fm, hydro_index=hydro_idx)

    counts = xref["match_method"].value_counts()
    total = len(xref)
    print(f"\nUsed rainfall stations: {total}")
    print("Match-method breakdown:")
    print(f"  {'method':<14} {'count':>5} {'percent':>8}")
    for m in ("reference", "coords", "name_exact", "name_fuzzy", "none"):
        n = int(counts.get(m, 0))
        pct = 100.0 * n / max(total, 1)
        print(f"  {m:<14} {n:>5d} {pct:>7.1f}%")

    matched = int((xref["match_method"] != "none").sum())
    print(f"\n{matched}/{total} used rainfall stations have a flood-monitoring counterpart")

    xref_path = ROOT / "data" / "processed" / "rainfall_monitoring_xref.csv"
    xref_path.parent.mkdir(parents=True, exist_ok=True)
    xref.to_csv(xref_path, index=False)
    print(f"\nXref written to {xref_path.relative_to(ROOT)}")

    print("\nSampling freshness on matched stations...")
    report = measure_freshness(xref, sample_n=25)
    report_path = ROOT / "outputs" / "rainfall_monitoring_coverage_report.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(report_path, index=False)
    print(f"Coverage report: {report_path.relative_to(ROOT)}")

    fresh = report[report["latest_age_minutes"].notna()]
    if not fresh.empty:
        med = fresh["latest_age_minutes"].median()
        print(f"\nSampled freshness — median age: {med:.0f} min ({med/60:.1f}h)")
        print(f"  <30 min:    {int((fresh['latest_age_minutes'] <= 30).sum())} of {len(fresh)}")
        print(f"  30-240 min: {int(((fresh['latest_age_minutes'] > 30) & (fresh['latest_age_minutes'] <= 240)).sum())} of {len(fresh)}")
        print(f"  >24h:       {int((fresh['latest_age_minutes'] > 1440).sum())} of {len(fresh)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
