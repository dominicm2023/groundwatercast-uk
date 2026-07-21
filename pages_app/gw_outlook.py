"""Forecast outlook — network-wide probabilistic groundwater forecast
(daily ENS to day 15; tiers keyed on the
operational 14-day window).

Surfaces the calibrated Pastas TFN forecast for every borehole in the live
scope. Two views over the same filtered set:
  • Triage — a worst-first ranked table (confidence-adjusted; stale-seed and
    proxy-threshold boreholes demoted/flagged).
  • Map — a Folium map coloured by forecast tier, hollow rings for stale seeds.
Selecting in either (table row / map marker) drives a shared rich detail panel:
current status vs normal, the stitched observed→fan trajectory, breach +
first-crossing, the incumbent-roll cross-check and model spread.
Indicative / uncalibrated.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from src.dashboard.ensemble_view import load_pastas, render_forecast_detail
from src.dashboard.forecast_outlook import (FRESH_SEED_MAX_DAYS,
                                            build_pastas_triage, TIER_LABEL)
from src.dashboard.map_builder import build_map
from src.dashboard.seasonal_view import load_seasonal, render_seasonal_outlook
from src.dashboard.status import (attach_current_status, load_normals,
                                  status_chip, TREND_ARROW)
from src.forecast.ensemble.thresholds import user_threshold_station_ids

_ROOT = Path(__file__).resolve().parents[1]
_CATALOGUE = _ROOT / "data" / "processed" / "catalogue.csv"
_SEL = "fc_selected"          # session-state key for the chosen borehole

# Forecast tier → the map's risk-index schema (so build_map's popup is coherent).
_TIER_RISK = {"BREACH_LIKELY": "HIGH", "BREACH_POSSIBLE": "MEDIUM",
              "WATCH": "MEDIUM", "STABLE": "LOW"}
_TIER_ACTION = {"BREACH_LIKELY": "IMMEDIATE_ACTION", "BREACH_POSSIBLE": "EMERGING_RISK",
                "WATCH": "EMERGING_RISK", "STABLE": "STABLE_LOW"}


@st.cache_data(show_spinner=False)
def _catalogue(_mtime: float) -> pd.DataFrame:
    return pd.read_csv(_CATALOGUE)


def _load_triage():
    summary, fan = load_pastas()
    if summary.empty:
        return summary, fan
    cat = _catalogue(_CATALOGUE.stat().st_mtime if _CATALOGUE.exists() else 0.0)
    tri = build_pastas_triage(summary, cat, set(user_threshold_station_ids()))
    tri = attach_current_status(tri, load_normals())
    return tri.reset_index(drop=True), fan


def _status_note(r: pd.Series) -> str:
    """One-line current-status summary for a triage row (popup + detail)."""
    chip = status_chip(r.get("status_now"), r.get("status_trend"),
                       r.get("status_percentile"))
    if r.get("status_now") is None or pd.isna(r.get("status_now")):
        return "Current level: no status (stale observation or no normals)"
    note = f"Current level: {chip}"
    age = r.get("status_age_days")
    if pd.notna(age):
        note += f" · obs {int(age)} d old"
    return note


def _threshold_label(r: pd.Series) -> str:
    if pd.isna(r["threshold"]):
        return "—"
    src = "proxy" if r["is_proxy"] else str(r["threshold_source"]).replace("_", " ")
    return f"{float(r['threshold']):.1f} ({src})"


def _map_snapshot(view: pd.DataFrame) -> pd.DataFrame:
    """Adapt the triage frame to the schema build_map expects. Marker colour
    stays the forecast TIER; the current vs-normal status only enriches the
    popup (the Trend row + a 'Current level' line in the reason text)."""
    notes = view.apply(_status_note, axis=1)
    return pd.DataFrame({
        "station_id": view["station_id"], "station_name": view["station_name"],
        "lat": view["lat"], "lon": view["lon"],
        "risk_raw": view["tier"].map(_TIER_RISK),
        # Marker size/score uses the OPERATIONAL 14-day window (p_breach_op),
        # matching both the tier colour and the "Breach 14d" table column — so a
        # marker can't look big from a full-horizon p_breach while the detail
        # panel shows a small 14-day figure.
        "risk_score": view["p_breach_op"].fillna(0.0),
        "data_age_days": view["stale_days"],
        # NOT is_live: "fresh seed (≤14d)" is not a ≤15-min live reading — leaving
        # is_live False gives solid-fill (fresh) vs hollow-ring (stale) styling
        # without falsely badging markers "LIVE".
        "is_live": False,
        "action_category": view["tier"].map(_TIER_ACTION),
        "reason_text": [f"{h} · {n}" if isinstance(h, str) and h else n
                        for h, n in zip(view["headline"], notes)],
        "trend": view["status_trend"].fillna("—"),
        "confidence_level": view["is_fresh"].map({True: "high", False: "low"}),
    }).dropna(subset=["lat", "lon"])


st.title("Forecast outlook")
st.caption(
    "Network-wide **probabilistic groundwater forecast** (calibrated Pastas "
    "TFN, PET-driven recharge) for every live-feed borehole plus any with a "
    "user-supplied threshold — daily ECMWF ENS to day 15 "
    "(tiers keyed on the first 14 days). **Indicative — uncalibrated** "
    "(calibration pending). Worst-first; stale-seeded boreholes are demoted "
    "and flagged."
)

tri, fan = _load_triage()
if tri.empty:
    st.info(
        "No forecast available yet. The 14-day outlook needs the Pastas "
        "forecast chain:\n\n"
        "1. one-off: create the dedicated env — `python -m venv .venv-pastas` "
        "then install `requirements-pastas.txt` into it\n"
        "2. `python -m scripts.refresh_pet --scope live` (PET cache)\n"
        "3. `python -m scripts.run_chain --ensemble --pastas`\n\n"
        "See the README section *“14-day forecast (optional extra setup)”*."
    )
    st.stop()

# KPI strip
k = st.columns(5)
k[0].metric("Boreholes", len(tri))
k[1].metric(f"Fresh (≤{FRESH_SEED_MAX_DAYS} d)", int(tri["is_fresh"].sum()))
k[2].metric("Stale-flagged", int((~tri["is_fresh"]).sum()))
k[3].metric("Breach ≥ 10%", int((tri["p_breach"].fillna(0) >= 0.10).sum()))
k[4].metric("User thresholds", int(tri["is_pinned"].sum()))

# Filters (apply to both views)
f = st.columns([1, 1, 1, 2])
fresh_only = f[0].toggle("Fresh seeds only", value=True,
                         help=f"Hide boreholes seeded > {FRESH_SEED_MAX_DAYS} days "
                              "ago (no live feed).")
real_only = f[1].toggle("Real thresholds only", value=False,
                        help="Exclude the gw_p90 proxy threshold.")
pinned_only = f[2].toggle("User-threshold BHs only", value=False)
query = f[3].text_input("Search borehole", "")

view = tri.copy()
if fresh_only:
    view = view[view["is_fresh"]]
if real_only:
    view = view[~view["is_proxy"]]
if pinned_only:
    view = view[view["is_pinned"]]
if query:
    view = view[view["station_name"].str.contains(query, case=False, na=False)]
view = view.reset_index(drop=True)

# View toggle — a radio, NOT st.tabs: tabs render both children with the
# inactive one display:none'd, and Leaflet initialising inside a hidden
# container sizes itself against width 0, so fit_bounds collapses to a
# world-zoom map. Rendering only the active view sidesteps that (and only
# one view does work per rerun).
view_mode = st.radio("View", ["📋 Triage", "🗺️ Map"], horizontal=True,
                     label_visibility="collapsed", key="outlook_view")

if view_mode == "📋 Triage":
    _h_full = int(view["horizon_days"].max()) if view["horizon_days"].notna().any() else 14
    _extended = _h_full > 14
    show = pd.DataFrame({
        "Borehole": view["station_name"],
        "Now": [status_chip(s, t)
                for s, t in zip(view["status_now"], view["status_trend"])],
        "Tier": view["tier"].map(TIER_LABEL),
        "Breach 14d": view["p_breach_op"].fillna(0.0),
        **({f"Breach {_h_full}d": view["p_breach"].fillna(0.0)} if _extended else {}),
        "Above P90 (14d)": view["p_secondary"].fillna(0.0),
        "First crossing": [pd.Timestamp(d).strftime("%d %b") if pd.notna(d) else "—"
                           for d in view["first_cross_median"]],
        "Seed": [f"{'🟢' if (pd.notna(d) and d <= FRESH_SEED_MAX_DAYS) else '🔴'} "
                 f"{int(d) if pd.notna(d) else '?'} d"
                 for d in view["stale_days"]],
        "Threshold": view.apply(_threshold_label, axis=1),
        "Spread (m)": view["model_spread_mean"],
        "Pinned": view["is_pinned"],
    })
    sel = st.dataframe(
        show, hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row", key="outlook_list",
        column_config={
            "Now": st.column_config.TextColumn(
                "Now", help="Current level vs this borehole's own normal for "
                            "the month (below/near/above the middle tercile) "
                            "+ 7-day trend arrow, from the freshest "
                            "observation incl. the live feed. ◻ = stale "
                            "observation or no normals."),
            "Breach 14d": st.column_config.ProgressColumn(
                "Breach 14d", min_value=0.0, max_value=1.0, format="%.0f%%",
                help="Operational window — drives the tier."),
            **({f"Breach {_h_full}d": st.column_config.ProgressColumn(
                f"Breach {_h_full}d", min_value=0.0, max_value=1.0, format="%.0f%%",
                help="Full extended horizon (EC46) — longer window, so "
                     "structurally higher; does not drive the tier.")}
               if _extended else {}),
            "Above P90 (14d)": st.column_config.ProgressColumn(
                "Above P90 (14d)", min_value=0.0, max_value=1.0, format="%.0f%%",
                help="P(level exceeds this month's P90 normal within 14 days) "
                     "— 'unusually high for the season'; the tier's secondary "
                     "signal."),
            "Spread (m)": st.column_config.NumberColumn("Spread (m)", format="%.2f"),
            "Pinned": st.column_config.CheckboxColumn(
                "Pinned", help="Has a user-supplied breach threshold "
                               "(data/thresholds/user_thresholds.yaml)."),
        },
    )
    st.caption(f"{len(view)} of {len(tri)} boreholes. Worst-first (confidence-adjusted: "
               "stale-seed & proxy-threshold boreholes demoted; current status breaks "
               "ties only). Columns sortable.")
    if sel and sel.selection and sel.selection.rows:
        st.session_state[_SEL] = view.iloc[sel.selection.rows[0]]["station_id"]

else:
    snap = _map_snapshot(view)
    if snap.empty:
        st.info("No boreholes with coordinates in the current filter.")
    else:
        map_data = st_folium(build_map(snap, max_data_age_days=FRESH_SEED_MAX_DAYS),
                             use_container_width=True, height=560,
                             returned_objects=["last_object_clicked",
                                               "last_object_clicked_popup"],
                             key="outlook_map")
        # Only act on a NEW marker click — st_folium replays the last click
        # on every rerun, which would otherwise clobber a fresh table-row
        # selection made before switching views.
        clicked = (map_data or {}).get("last_object_clicked")
        if clicked and clicked != st.session_state.get("_fc_last_click"):
            st.session_state["_fc_last_click"] = clicked
            if "lat" in clicked and "lng" in clicked:
                mask = ((snap["lat"].sub(float(clicked["lat"])).abs() < 1e-5)
                        & (snap["lon"].sub(float(clicked["lng"])).abs() < 1e-5))
                hits = snap.loc[mask, "station_id"]
                if not hits.empty:
                    # Co-located stations stack their markers (see
                    # scripts/survey_duplicate_stations.py), so a click can match
                    # several rows. `snap` preserves `view`'s triage order
                    # (worst-first), so iloc[0] picks the operationally worst.
                    st.session_state[_SEL] = str(hits.iloc[0])
        st.caption("Marker colour & size = forecast tier; hollow ring = stale seed "
                   f"(> {FRESH_SEED_MAX_DAYS} d, no live feed). Popups include the "
                   "current risk-index band. Click a marker for its forecast detail.")

# Shared detail panel (driven by table row or map marker). Gated to the current
# filtered view so a selection that's been filtered out doesn't linger.
st.markdown("---")
sid = st.session_state.get(_SEL)
if sid is not None and sid in view["station_id"].values:
    r = view[view["station_id"] == sid].iloc[0]
    fsub = fan[fan["station_id"] == sid].sort_values("lead")
    # Combined-product header: current operational state first, then the
    # forecast detail below — band-now → trend → fan (+ seasonal monthly
    # envelope continuing the arc) → breach prob.
    st.markdown(f"**{_status_note(r)}**")
    _seasonal_all = load_seasonal()
    _seasonal = (_seasonal_all[_seasonal_all["station_id"] == sid]
                 if not _seasonal_all.empty else None)
    render_forecast_detail(sid, r, fsub, name=r["station_name"],
                           key_prefix="outlook", seasonal=_seasonal)
    # Months 1-6 tercile outlook (experimental; explains itself when absent).
    render_seasonal_outlook(sid)
else:
    st.info("Select a borehole — in the triage table or on the map — to see its "
            "forecast detail (stitched observed → 14-day fan, roll cross-check, "
            "breach & above-normal probabilities, first-crossing).")
