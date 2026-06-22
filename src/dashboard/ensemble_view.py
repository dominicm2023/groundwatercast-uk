"""Forward-outlook (probabilistic ensemble) rendering for the outlook page.

Surfaces the 14-day probabilistic GW forecast for a borehole.
Primary = the **calibrated Pastas TFN** forecast (PET-driven FlexModel recharge):
its fan (P10/P50/P90 = member spread + calibrated noise band), breach probability,
first-crossing distribution, plus the incumbent reduced-form **roll P50 overlaid as
a cross-check** and a roll↔Pastas model-spread metric. A `stale_days` badge flags
boreholes seeded at an old observation (no live feed). Falls back to the roll-only
ensemble view for boreholes without a calibrated Pastas model. All numbers are
*indicative / uncalibrated* (design §9; Phase C calibration pending).

Data (cached, keyed on file mtime):
  Pastas : data/model/forecast_pastas_summary.csv + forecast_pastas_fan.csv
  Roll   : data/model/forecast_ensemble_summary.csv + forecast_ensemble_fan.csv
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.dashboard.forecast_outlook import FRESH_SEED_MAX_DAYS

_ROOT = Path(__file__).parents[2]
_SUMMARY = _ROOT / "data" / "model" / "forecast_ensemble_summary.csv"
_FAN = _ROOT / "data" / "model" / "forecast_ensemble_fan.csv"
_PSUMMARY = _ROOT / "data" / "model" / "forecast_pastas_summary.csv"
_PFAN = _ROOT / "data" / "model" / "forecast_pastas_fan.csv"

_BAND = "rgba(31, 119, 180, 0.18)"
_MEDIAN = "#1f77b4"
_THRESHOLD = "#ff7f0e"
_ROLL = "#7f7f7f"                       # incumbent roll cross-check line
_STALE_DAYS = FRESH_SEED_MAX_DAYS       # badge threshold (single-sourced)
# Leads beyond this are extended-range forcing (EC46) — daily skill is weak
# there, the cross-member envelope is the signal. Matches
# config forecast.ensemble.extended.splice_day.
EXTENDED_FROM_LEAD = 15


def _mark_extended_range(fig: go.Figure, fsub: pd.DataFrame) -> None:
    """Visually separate the extended-range leads (> EXTENDED_FROM_LEAD):
    light grey wash + dotted boundary + label. No-op for 14-day fans."""
    if fsub.empty or "lead" not in fsub.columns:
        return
    if int(fsub["lead"].max()) <= EXTENDED_FROM_LEAD:
        return
    ext = fsub[fsub["lead"] > EXTENDED_FROM_LEAD]
    x0, x1 = ext["date"].min(), fsub["date"].max()
    fig.add_vrect(x0=x0, x1=x1, fillcolor="rgba(127,127,127,0.08)",
                  line_width=0, layer="below")
    fig.add_vline(x=x0, line_dash="dot", line_color="#9aa0a6")
    fig.add_annotation(x=x0, yref="paper", y=0.02, text="extended range (EC46)",
                       showarrow=False, xanchor="left", yanchor="bottom",
                       font=dict(size=9, color="#7f7f7f"))


def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else -1.0


@st.cache_data(show_spinner=False)
def _load_impl(_sv: float, _fv: float):
    summary = pd.read_csv(_SUMMARY) if _SUMMARY.exists() else pd.DataFrame()
    fan = pd.read_csv(_FAN) if _FAN.exists() else pd.DataFrame()
    if not fan.empty:
        fan["date"] = pd.to_datetime(fan["date"])
    return summary, fan


@st.cache_data(show_spinner=False)
def _load_pastas_impl(_sv: float, _fv: float):
    summary = pd.read_csv(_PSUMMARY) if _PSUMMARY.exists() else pd.DataFrame()
    fan = pd.read_csv(_PFAN) if _PFAN.exists() else pd.DataFrame()
    if not fan.empty:
        fan["date"] = pd.to_datetime(fan["date"])
    return summary, fan


def load_ensemble():
    """(summary_df, fan_df) for the roll; empty when absent. Cached."""
    return _load_impl(_mtime(_SUMMARY), _mtime(_FAN))


def load_pastas():
    """(summary_df, fan_df) for the Pastas forecast; empty when absent. Cached."""
    return _load_pastas_impl(_mtime(_PSUMMARY), _mtime(_PFAN))


def _fan_figure(fsub: pd.DataFrame, row: pd.Series,
                roll_p50: pd.Series | None = None) -> go.Figure:
    """Fan figure. With ``roll_p50`` (the incumbent roll median) a dashed
    cross-check line is overlaid; without it the figure is the roll-only fan
    (3 traces) for backward compatibility."""
    fig = go.Figure()
    x = fsub["date"]
    fig.add_trace(go.Scatter(x=x, y=fsub["gw_p90"], mode="lines",
                             line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=fsub["gw_p10"], mode="lines",
                             line=dict(width=0), fill="tonexty", fillcolor=_BAND,
                             name="P10–P90", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=fsub["gw_p50"], mode="lines", name="median",
                             line=dict(color=_MEDIAN, width=2.0),
                             hovertemplate="%{x|%d %b} · %{y:.2f} mAOD<extra></extra>"))
    if roll_p50 is not None and pd.notna(roll_p50).any():
        fig.add_trace(go.Scatter(x=x, y=roll_p50, mode="lines", name="roll (cross-check)",
                                 line=dict(color=_ROLL, width=1.6, dash="dot"),
                                 hovertemplate="roll %{y:.2f} mAOD<extra></extra>"))
    if pd.notna(row.get("threshold")):
        src = row.get("threshold_source", "")
        label = "threshold" + (" (proxy)" if src == "gw_p90_proxy" else "")
        fig.add_hline(y=float(row["threshold"]), line_dash="dash",
                      line_color=_THRESHOLD,
                      annotation_text=f"{label}: {float(row['threshold']):g} mAOD",
                      annotation_position="top left", annotation_font_size=10)
    _mark_extended_range(fig, fsub)
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title="Forecast date", yaxis_title="GW level (mAOD)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def _pct(p) -> str:
    """Percentage that never rounds a small-but-real probability to "0%"
    (or a near-certainty to "100%") — a couple of MC samples crossing is
    "<1%", not a flat zero sitting next to a quoted crossing date."""
    if pd.isna(p):
        return "—"
    p = float(p)
    if p == 0.0:
        return "0%"
    if p < 0.01:
        return "<1%"
    if 0.99 < p < 1.0:
        return ">99%"
    return f"{p:.0%}"


# Below this, first-crossing dates are sampling noise (a handful of MC
# trajectories grazing the threshold) — show "—" instead of implying a
# meaningful crossing-date distribution.
_FIRST_CROSS_MIN_P = 0.01


def _first_cross_value(row: pd.Series) -> str:
    fcm = row.get("first_cross_median")
    p = row.get("p_breach")
    if pd.isna(fcm) or pd.isna(p) or float(p) < _FIRST_CROSS_MIN_P:
        return "—"
    return pd.Timestamp(fcm).strftime("%d %b")


def _breach_label_value(row: pd.Series) -> tuple[str, str, str | None]:
    """(label, value, help) for the breach metric. Dual-window horizons show
    the tier-driving 14-day probability as the BIG number; the full-horizon
    figure lives in the help tooltip and the panel caption — both a combined
    value ("42% / 64%") and a long label truncate in the metric column."""
    h = int(row["horizon_days"])
    pb = row["p_breach"]
    pb14 = row.get("p_breach_14d")
    if h > 14 and pd.notna(pb14):
        return ("Breach prob (14 d)", _pct(pb14),
                f"Within the full {h}-day horizon: {_pct(pb)}. The tier is "
                f"driven by the 14-day window shown.")
    return f"Breach prob ({h} d)", _pct(pb), None


def _horizon_caption(row: pd.Series) -> str | None:
    """One caption line carrying the full-horizon breach figure (never
    truncates, unlike the metric column)."""
    h = int(row.get("horizon_days") or 0)
    pb = row.get("p_breach")
    pb14 = row.get("p_breach_14d")
    if h > 14 and pd.notna(pb) and pd.notna(pb14):
        return f"Full {h}-day horizon: {_pct(pb)} breach probability."
    return None


def _breach_metrics(row: pd.Series, *, extra_spread: float | None = None) -> None:
    if not pd.notna(row.get("p_breach")):
        return
    cols = st.columns(4 if extra_spread is not None else 3)
    blabel, bval, bhelp = _breach_label_value(row)
    cols[0].metric(blabel, bval, help=bhelp)
    cols[1].metric("Threshold (mAOD)", f"{float(row['threshold']):.1f}",
                   row.get("threshold_source", ""))
    cols[2].metric("Median first crossing", _first_cross_value(row))
    if extra_spread is not None and pd.notna(extra_spread):
        cols[3].metric("Model spread (roll↔Pastas)", f"{extra_spread:.2f} m")


_OBS = "#2b2b2b"          # observed GW line


def _load_observed(sid: str, days: int = 1100) -> pd.DataFrame:
    """Recent observed daily GW (the run-up behind the forecast), or empty.

    Deliberately NOT truncated at the forecast origin: observations newer
    than the seed (live tail landed since the members run) plot alongside
    the fan, so the reader sees actual-vs-forecast instead of an artificial
    gap between the end of the line and the start of the fan."""
    try:
        from src.dashboard.loaders import load_gw_for_bh
        df = load_gw_for_bh(sid)
    except Exception:
        return pd.DataFrame(columns=["date", "GW_Level"])
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "GW_Level"])
    df = df.rename(columns={c: "date" for c in df.columns if "date" in c.lower()}).copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").tail(days)[["date", "GW_Level"]]


_SEASONAL_BAND = "rgba(127, 127, 180, 0.10)"
_SEASONAL_LINE = "#5b6abf"


def _add_seasonal_extension(fig: go.Figure,
                            seasonal: pd.DataFrame | None) -> None:
    """Continue the chart beyond the daily fan with the months-1..6 ESP
    envelope: monthly-mean P10/P50/P90 at month midpoints — dotted line +
    circular markers (coarse markers = monthly means, not daily levels).
    Silently a no-op when there's nothing usable."""
    if seasonal is None or seasonal.empty:
        return
    need = {"month_start", "gw_p10", "gw_p50", "gw_p90"}
    if not need.issubset(seasonal.columns):
        return
    s = seasonal.dropna(subset=["gw_p50"]).sort_values("month_start")
    if s.empty:
        return
    x = pd.to_datetime(s["month_start"]) + pd.Timedelta(days=14)
    fig.add_trace(go.Scatter(x=x, y=s["gw_p90"], mode="lines",
                             line=dict(width=0), showlegend=False,
                             hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=s["gw_p10"], mode="lines",
                             line=dict(width=0), fill="tonexty",
                             fillcolor=_SEASONAL_BAND,
                             name="seasonal P10–P90", hoverinfo="skip"))
    fig.add_trace(go.Scatter(
        x=x, y=s["gw_p50"], mode="lines+markers",
        name="seasonal median (monthly)",
        line=dict(color=_SEASONAL_LINE, width=1.6, dash="dot"),
        marker=dict(size=7, symbol="circle-open",
                    line=dict(width=1.6, color=_SEASONAL_LINE)),
        hovertemplate="%{x|%b %Y} · monthly mean %{y:.2f} mAOD"
                      "<extra>seasonal (ESP)</extra>"))


def stitched_figure(fsub: pd.DataFrame, row: pd.Series,
                    observed: pd.DataFrame | None,
                    seasonal: pd.DataFrame | None = None) -> go.Figure:
    """The flagship trajectory: recent observed GW → seed marker → P10/P50/P90
    fan + dotted roll cross-check + dashed threshold (+ optionally the
    months-1..6 seasonal envelope continuing the arc). The seed line sits at
    the forecast ORIGIN (the observation the forecast was seeded at), so a
    stale seed is geometrically obvious — and observations newer than the
    seed plot alongside the fan as an actual-vs-forecast check."""
    fig = _fan_figure(fsub, row, roll_p50=fsub["roll_p50"] if "roll_p50" in fsub else None)
    _add_seasonal_extension(fig, seasonal)
    # Default view: recent observed run-up → end of the forecast arc; the
    # multi-year history loaded above is one range-button away.
    x_end = pd.to_datetime(fsub["date"]).max() if not fsub.empty else None
    if seasonal is not None and not seasonal.empty and "month_start" in seasonal:
        s_end = pd.to_datetime(seasonal["month_start"]).max() + pd.Timedelta(days=20)
        x_end = max(x_end, s_end) if x_end is not None else s_end
    if observed is not None and not observed.empty and x_end is not None:
        view_start = pd.to_datetime(observed["date"]).max() - pd.Timedelta(days=130)
        fig.update_xaxes(
            range=[view_start, x_end + pd.Timedelta(days=7)],
            rangeselector=dict(
                buttons=[
                    dict(count=6, label="6m", step="month", stepmode="backward"),
                    dict(count=1, label="1y", step="year", stepmode="backward"),
                    dict(count=2, label="2y", step="year", stepmode="backward"),
                    dict(step="all", label="All"),
                ],
                x=0, xanchor="left", y=1.12, yanchor="bottom",
                font=dict(size=10),
            ))
    if observed is not None and not observed.empty:
        fig.add_trace(go.Scatter(x=observed["date"], y=observed["GW_Level"],
                                 mode="lines", name="observed",
                                 line=dict(color=_OBS, width=1.6),
                                 hovertemplate="%{x|%d %b} · %{y:.2f} mAOD<extra>obs</extra>"))
        # Seed line at the forecast origin (fall back to the last observation
        # when the summary lacks one, e.g. the roll-only schema).
        # NB: add_vline's inline annotation breaks on datetime x (plotly computes
        # a mean of the Timestamp) — draw the line, then place the label separately.
        origin = row.get("origin_date")
        seed = pd.Timestamp(origin) if pd.notna(origin) else observed["date"].max()
        fig.add_vline(x=seed, line_dash="dot", line_color="#9aa0a6")
        fig.add_annotation(x=seed, yref="paper", y=1.0, text="seed", showarrow=False,
                           xanchor="left", yanchor="bottom",
                           font=dict(size=9, color="#9aa0a6"))
    return fig


def render_forecast_detail(sid: str, row: pd.Series, fsub: pd.DataFrame, *,
                           name: str | None = None, key_prefix: str = "fc",
                           seasonal: pd.DataFrame | None = None) -> None:
    """Rich single-borehole forecast detail — shared by the Forecast-outlook page
    and any other page that embeds a borehole detail. Stitched trajectory +
    metrics + honesty flags."""
    name = name or row.get("station_name") or sid[:8]
    st.markdown(f"#### {name}")
    if row.get("headline"):
        st.markdown(str(row["headline"]))
    stale = row.get("stale_days")
    if pd.notna(stale) and float(stale) > _STALE_DAYS:
        st.warning(f"⚠ Seeded at the last observation **{int(stale)} days ago** "
                   "(no live feed) — forecast and roll↔Pastas spread increasingly uncertain.")
    elif pd.notna(stale):
        st.caption(f"Seeded {int(stale)} days ago (fresh).")

    if pd.notna(row.get("p_breach")):
        c = st.columns(5)
        blabel, bval, bhelp = _breach_label_value(row)
        c[0].metric(blabel, bval, help=bhelp)
        prh = row.get("p_above_p90_14d", row.get("p_risk_high"))
        c[1].metric("P(above P90, 14 d)", _pct(prh),
                    help="Chance the level is unusually high for the season "
                         "(above this month's P90 normal) within 14 days — "
                         "the tier's secondary signal.")
        thr = row.get("threshold")
        c[2].metric("Threshold (mAOD)", f"{float(thr):.1f}" if pd.notna(thr) else "—",
                    row.get("threshold_source", "") or "")
        c[3].metric("Median first crossing", _first_cross_value(row))
        sp = row.get("model_spread_mean")
        c[4].metric("Model spread (roll↔Pastas)", f"{float(sp):.2f} m" if pd.notna(sp) else "—")

    if fsub is None or fsub.empty:
        st.info("No forecast fan available for this borehole.")
        return
    view = st.radio("Chart view", ["📈 Trajectory", "🍂 Season view"],
                    horizontal=True, label_visibility="collapsed",
                    key=f"{key_prefix}_view_{sid[:8]}")
    season_mode = view.endswith("Season view")
    if season_mode:
        from src.dashboard.season_view import render_season_view
        render_season_view(sid, fsub, seasonal,
                           key_prefix=f"{key_prefix}_season")
    else:
        obs = _load_observed(sid)
        st.plotly_chart(stitched_figure(fsub, row, obs, seasonal=seasonal),
                        width="stretch", key=f"{key_prefix}_{sid[:8]}")

    a, m, b = row.get("first_cross_p25"), row.get("first_cross_median"), row.get("first_cross_p75")
    pb = row.get("p_breach")
    hcap = _horizon_caption(row)
    if pd.notna(m) and pd.notna(pb) and float(pb) >= _FIRST_CROSS_MIN_P:
        cf = row.get("censored_frac", 0) or 0
        st.caption((f"{hcap} " if hcap else "")
                   + f"First crossing — P25–median–P75: {pd.Timestamp(a):%d %b} – "
                   f"{pd.Timestamp(m):%d %b} – {pd.Timestamp(b):%d %b}; "
                   f"{_pct(cf)} of members never cross within the horizon.")
    elif hcap:
        st.caption(hcap)
    if not season_mode:
        st.caption("**Indicative — uncalibrated** (Phase C pending). Solid blue = Pastas "
                   "median · dotted grey = incumbent roll (cross-check) · dashed orange = threshold · "
                   "black = observed"
                   + (" · dotted circles = seasonal monthly-mean median (ESP, "
                      "experimental)" if seasonal is not None and not seasonal.empty
                      else "") + ".")


