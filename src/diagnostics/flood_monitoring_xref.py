"""
Cross-reference our hydrology-API station catalogue with the EA
flood-monitoring API's groundwater stations.

Why
---
The hydrology API (which the pipeline already uses) is the *audited
historical archive*; it lags by weeks or months.  The flood-monitoring
API is the EA's near-real-time feed, updated every 15 minutes from the
same underlying telemetry.  Boreholes that appear in both can have
their dashboard tail extended to "now" without retraining the model
(see ``src.forecast.live_levels``).

The two APIs use different station IDs (GUIDs vs short codes), so this
module builds a mapping between them.  The mapping is written to
``data/processed/flood_monitoring_xref.csv`` and a coverage summary to
``outputs/flood_monitoring_coverage_report.csv``.

Match strategy (in order, first hit wins)
-----------------------------------------
1. ``stationReference``  : when both APIs expose the same EA reference
                           code on the same station, it's the truth.
2. ``coords``            : within 50 m haversine when both have lat/lng.
3. ``name_exact``        : case-insensitive whitespace-normalised match.
4. ``name_fuzzy``        : RapidFuzz token-sort ratio ≥ 90 (manually
                           reviewable from the xref CSV).
5. ``none``              : no match found.

Run with:
    python -m src.diagnostics.flood_monitoring_xref
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path

import pandas as pd
import requests

from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).parents[2]

_FM_BASE = "https://environment.data.gov.uk/flood-monitoring"
_LIST_URL = _FM_BASE + "/id/stations"
_HYDRO_LIST_URL = "https://environment.data.gov.uk/hydrology/id/stations.json"

_COORD_TOLERANCE_M:  float = 50.0
_FUZZY_THRESHOLD:    int   = 90
_REQUEST_TIMEOUT_S:  int   = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (small-distance accurate)."""
    R = 6_371_000.0
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _norm_name(s: object) -> str:
    """Lower-case, alnum-only, whitespace-collapsed."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(s).lower())).strip()


def _fuzzy_ratio(a: str, b: str) -> int:
    """Token-sort ratio without RapidFuzz dependency (small inputs only)."""
    # Simple implementation: sort tokens, then SequenceMatcher ratio.
    from difflib import SequenceMatcher
    aa = " ".join(sorted(a.split()))
    bb = " ".join(sorted(b.split()))
    return int(round(SequenceMatcher(None, aa, bb).ratio() * 100))


# ---------------------------------------------------------------------------
# Flood-monitoring API fetcher
# ---------------------------------------------------------------------------

def fetch_flood_monitoring_gw_stations() -> pd.DataFrame:
    """
    Fetch every groundwater-qualifier station from the flood-monitoring API.

    Returns a DataFrame with one row per (station, measure) pair.  Some
    rows may have null lat/lon because the listing endpoint doesn't
    include coordinates for every station.
    """
    r = requests.get(
        _LIST_URL,
        params={"qualifier": "Groundwater", "_limit": 10_000},
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
            "fm_notation":         notation,
            "fm_label":            label,
            "fm_station_reference": ref,
            "fm_lat":              it.get("lat"),
            "fm_lon":              it.get("long"),
            "fm_status":           it.get("status"),
            "fm_date_opened":      it.get("dateOpened"),
        })
    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# Cross-reference builder
# ---------------------------------------------------------------------------

def fetch_hydrology_reference_index() -> dict[str, dict]:
    """
    Fetch the full hydrology stations listing and return a dict keyed by
    ``stationGuid`` with the EA-side reference fields we'll use for
    matching to flood-monitoring (``stationReference``, ``wiskiID``,
    ``notation``, plus ``label``).
    """
    r = requests.get(_HYDRO_LIST_URL, params={"_limit": 20_000}, timeout=120)
    r.raise_for_status()
    items = r.json().get("items", [])
    out: dict[str, dict] = {}
    for s in items:
        guid = s.get("stationGuid")
        if not guid or isinstance(guid, (list, dict)):
            continue
        out[str(guid)] = {
            "label": s.get("label"),
            "stationReference": s.get("stationReference"),
            "wiskiID": s.get("wiskiID"),
            "notation": s.get("notation"),
        }
    return out


def _refs_for_hydro_station(h: dict) -> set[str]:
    """Collect all valid EA reference fields from a hydrology station record."""
    refs: set[str] = set()
    for key in ("stationReference", "wiskiID", "notation"):
        v = (h or {}).get(key)
        if v and not isinstance(v, (list, dict)):
            refs.add(str(v).strip().upper())
    refs.discard("")
    return refs


def build_xref(
    hydro_catalogue_path: Path | None = None,
    fm_stations: pd.DataFrame | None = None,
    hydro_index: dict[str, dict] | None = None,
) -> pd.DataFrame:
    """
    Build the cross-reference between hydrology-API stations and
    flood-monitoring-API stations.

    Returns one row per hydrology station with a ``match_method`` column
    (``reference``, ``coords``, ``name_exact``, ``name_fuzzy``, ``none``).
    """
    hydro_catalogue_path = hydro_catalogue_path or (
        ROOT / "data" / "processed" / "catalogue.csv"
    )
    cat = pd.read_csv(hydro_catalogue_path)
    gw = (
        cat[cat["measure_type"] == "groundwater"]
        [["station_id", "station_name", "lat", "lon"]]
        .drop_duplicates("station_id")
        .reset_index(drop=True)
    )

    if fm_stations is None:
        fm_stations = fetch_flood_monitoring_gw_stations()
    if hydro_index is None:
        hydro_index = fetch_hydrology_reference_index()

    # Pre-compute normalised names for both sides
    gw["_norm_name"] = gw["station_name"].map(_norm_name)
    fm_stations = fm_stations.copy()
    fm_stations["_norm_label"] = fm_stations["fm_label"].map(_norm_name)

    # Build FM reference index for O(1) lookup by any reference field
    fm_by_ref: dict[str, pd.Series] = {}
    for _, fm_row in fm_stations.iterrows():
        for key in ("fm_notation", "fm_station_reference"):
            v = fm_row.get(key)
            if v and not isinstance(v, (list, dict)):
                fm_by_ref.setdefault(str(v).strip().upper(), fm_row)

    # Split FM stations into ones with coords and ones without — the coord
    # match step only considers the former.
    fm_with_coords = fm_stations.dropna(subset=["fm_lat", "fm_lon"]).copy()

    results: list[dict] = []
    for _, row in gw.iterrows():
        sid = row["station_id"]
        sname = row["station_name"]
        slat, slon = row["lat"], row["lon"]
        snorm = row["_norm_name"]

        match: dict | None = None

        # 1. Reference match via the hydrology listing's stationReference /
        # wiskiID / notation fields — the strongest signal when present.
        h = hydro_index.get(str(sid))
        if h:
            hydro_refs = _refs_for_hydro_station(h)
            for ref in hydro_refs:
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
    reading and compute its age.  Returns the matched DataFrame with two
    extra columns: ``latest_reading``, ``latest_age_minutes``.
    """
    matched = xref[xref["fm_notation"].notna()].copy()
    if matched.empty:
        matched["latest_reading"] = pd.NaT
        matched["latest_age_minutes"] = pd.NA
        return matched

    # Random sample to avoid hammering the API for the report
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
            if items:
                latest[notation] = pd.Timestamp(items[0]["dateTime"]).tz_convert("UTC")
            else:
                latest[notation] = None
        except Exception:
            latest[notation] = None
        time.sleep(0.05)  # courtesy spacing

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
    print("Fetching flood-monitoring groundwater stations...")
    fm = fetch_flood_monitoring_gw_stations()
    print(f"  {len(fm)} flood-monitoring GW stations nationwide")
    print(f"  {fm['fm_lat'].notna().sum()} have coordinates in the listing")

    print("\nFetching hydrology API reference index...")
    hydro_idx = fetch_hydrology_reference_index()
    print(f"  {len(hydro_idx)} hydrology stations indexed by GUID")

    print("\nBuilding cross-reference against the hydrology catalogue...")
    xref = build_xref(fm_stations=fm, hydro_index=hydro_idx)

    counts = xref["match_method"].value_counts()
    total = len(xref)
    print("\nMatch-method breakdown:")
    print(f"  {'method':<14} {'count':>5} {'percent':>8}")
    for m in ("reference", "coords", "name_exact", "name_fuzzy", "none"):
        n = int(counts.get(m, 0))
        pct = 100.0 * n / max(total, 1)
        print(f"  {m:<14} {n:>5d} {pct:>7.1f}%")

    matched = int((xref["match_method"] != "none").sum())
    print(f"\n{matched}/{total} hydrology stations have a flood-monitoring counterpart")

    # Persist
    xref_path = ROOT / "data" / "processed" / "flood_monitoring_xref.csv"
    xref_path.parent.mkdir(parents=True, exist_ok=True)
    xref.to_csv(xref_path, index=False)
    print(f"\nXref written to {xref_path.relative_to(ROOT)}")

    # Freshness sample for the coverage report
    print("\nSampling freshness on matched stations...")
    report = measure_freshness(xref, sample_n=25)
    report_path = ROOT / "outputs" / "flood_monitoring_coverage_report.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(report_path, index=False)
    print(f"Coverage report: {report_path.relative_to(ROOT)}")

    fresh = report[report["latest_age_minutes"].notna()]
    if not fresh.empty:
        med = fresh["latest_age_minutes"].median()
        print(f"\nSampled freshness — median age: {med:.0f} min ({med/60:.1f}h)")
        print(f"  <30 min:   {int((fresh['latest_age_minutes'] <= 30).sum())} of {len(fresh)}")
        print(f"  30–240 min:{int(((fresh['latest_age_minutes'] > 30) & (fresh['latest_age_minutes'] <= 240)).sum())} of {len(fresh)}")
        print(f"  >24h:      {int((fresh['latest_age_minutes'] > 1440).sum())} of {len(fresh)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
