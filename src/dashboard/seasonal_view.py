"""Seasonal outlook rendering — monthly tercile bars (experimental).

Reads forecast_seasonal_summary.csv (built monthly by
scripts/build_seasonal_outlook.py) and renders, for one borehole, the
months-1..6 P(below / near / above normal GW) as stacked horizontal bars
plus a one-line reading. Quietly absent when the artifact is missing.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).parents[2]
_SUMMARY = _ROOT / "data" / "model" / "forecast_seasonal_summary.csv"

_COLORS = {"below": "#d4a017", "near": "#b5b5b5", "above": "#1f77b4"}
_LABELS = {"below": "below normal", "near": "near normal", "above": "above normal"}


def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else -1.0


@st.cache_data(show_spinner=False)
def _load_impl(_v: float) -> pd.DataFrame:
    if not _SUMMARY.exists():
        return pd.DataFrame()
    df = pd.read_csv(_SUMMARY)
    df["month_start"] = pd.to_datetime(df["month_start"])
    return df


def load_seasonal() -> pd.DataFrame:
    """The seasonal summary (empty when not yet built). mtime-cached."""
    return _load_impl(_mtime(_SUMMARY))


def _bars_figure(sub: pd.DataFrame) -> go.Figure:
    sub = sub.sort_values("month_ahead", ascending=False)   # month 1 on top
    ylabels = [pd.Timestamp(d).strftime("%b %Y") for d in sub["month_start"]]
    fig = go.Figure()
    for key in ("below", "near", "above"):
        fig.add_trace(go.Bar(
            y=ylabels, x=sub[f"p_{key}"], orientation="h",
            name=_LABELS[key], marker_color=_COLORS[key],
            hovertemplate="%{y} · " + _LABELS[key] + ": %{x:.0%}<extra></extra>",
        ))
    fig.update_layout(
        barmode="stack", height=40 + 28 * len(sub),
        margin=dict(l=10, r=10, t=8, b=8),
        xaxis=dict(tickformat=".0%", range=[0, 1], title=None),
        yaxis=dict(title=None),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, font=dict(size=10)),
    )
    return fig


def _headline(sub: pd.DataFrame) -> str | None:
    """The single strongest statement worth a sentence (lead tercile ≥ 50%
    in any month), or None when the outlook is flat."""
    best = None
    for _, r in sub.iterrows():
        for key in ("below", "near", "above"):
            p = float(r[f"p_{key}"])
            if p >= 0.5 and (best is None or p > best[0]):
                best = (p, key, pd.Timestamp(r["month_start"]))
    if best is None:
        return None
    p, key, month = best
    return (f"Strongest signal: **{month:%B %Y} — {p:.0%} likely "
            f"{_LABELS[key]}** for this borehole.")


def render_seasonal_outlook(sid: str) -> None:
    """Seasonal tercile section for the detail panel (no-op-safe)."""
    df = load_seasonal()
    if df.empty:
        st.caption("Seasonal outlook not built yet — run "
                   "`python -m scripts.run_chain --seasonal` (monthly).")
        return
    sub = df[df["station_id"] == sid]
    if sub.empty:
        # In-scope boreholes can legitimately lack rows (input caches still
        # backfilling, or too few usable trace years) — say so rather than
        # silently looking like a missing feature.
        st.caption("Seasonal outlook (months 1–6): not available for this "
                   "borehole yet — its trace inputs are still backfilling "
                   "or it has too short a history. Rebuilds with "
                   "`run_chain --seasonal`.")
        return
    st.markdown("##### Seasonal outlook (experimental)")
    fig = _bars_figure(sub)
    st.plotly_chart(fig, width="stretch", key=f"seasonal_{sid[:8]}")
    head = _headline(sub)
    if head:
        st.markdown(head)
    n = int(sub["n_traces"].iloc[0])
    w = bool(sub["seas5_weighted"].iloc[0])
    st.caption(
        f"P(below / near / above normal monthly-mean GW) vs this borehole's "
        f"own {n}-year-trace climatology — ESP historic-year traces through "
        f"the calibrated model"
        + (", weighted by ECMWF SEAS5 monthly rainfall terciles" if w
           else " (equal trace weights)")
        + ". **Experimental — unverified**; treat as climatological context, "
          "not a prediction.")
