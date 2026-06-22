"""Season view — year-on-year hydrograph overlay for one borehole.

The classic groundwater comparison: this year's observed + forecast drawn
over previous years' traces, aligned by day-of-year (calendar Jan–Dec or
water year Oct–Sep), with a historical P10–P90 daily envelope.

Wrap handling: nothing wraps. Every dated point gets an
``(alignment_year, axis_day)`` pair and every series is split into
per-alignment-year segments plotted independently — a Jun→Dec forecast
under water-year alignment simply becomes two segments, the Oct–Dec part
appearing at the left edge as its own labelled trace.
"""
from __future__ import annotations

import pandas as pd

# plotly / streamlit are imported lazily inside the figure/render functions so
# the pure alignment math (align_series, daily_envelope, …) is importable — and
# unit-testable — without a plotting stack installed.

CALENDAR = "calendar"
WATER = "water"            # UK water year: 1 Oct → 30 Sep

_PAST = "#9aa0a6"
_ENVELOPE = "rgba(127, 127, 127, 0.12)"
_NOW = "#2b2b2b"
_FORECAST = "#1f77b4"
_SEASONAL = "#5b6abf"

# Month tick positions (non-leap reference): axis_day of each month start.
_CAL_TICKS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
_CAL_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WATER_TICKS = [0, 31, 61, 92, 123, 151, 182, 212, 243, 273, 304, 335]
_WATER_LABELS = ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar",
                 "Apr", "May", "Jun", "Jul", "Aug", "Sep"]

# Cumulative days before each month on a fixed non-leap (365-day) calendar —
# matches the tick reference above.
_MONTH_CUM = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def _canonical_doy(ts: pd.Timestamp) -> int:
    """0-based day-of-year on a fixed non-leap calendar, with 29 Feb folded
    onto 28 Feb. The point of the season view is to align the *same calendar
    date* across years; a raw ``(date - year_start).days`` shifts every
    post-February date by one in leap years, so 15 Mar 2024 would land a day
    right of 15 Mar 2023 in both the overlay and the P10–P90 envelope. Folding
    onto a 365-day calendar keeps every year's dates on the same axis position
    (and on the non-leap month ticks)."""
    ts = pd.Timestamp(ts)
    day = 28 if (ts.month == 2 and ts.day == 29) else ts.day
    return _MONTH_CUM[ts.month - 1] + (day - 1)


def year_start(ts: pd.Timestamp, alignment: str) -> pd.Timestamp:
    """Start of the alignment year containing ``ts``."""
    ts = pd.Timestamp(ts)
    if alignment == WATER:
        y = ts.year if ts.month >= 10 else ts.year - 1
        return pd.Timestamp(year=y, month=10, day=1)
    return pd.Timestamp(year=ts.year, month=1, day=1)


def year_label(ts: pd.Timestamp, alignment: str) -> str:
    """Display label of the alignment year containing ``ts``
    ("2024" or "WY2025" = Oct 2024–Sep 2025)."""
    start = year_start(ts, alignment)
    return f"WY{start.year + 1}" if alignment == WATER else str(start.year)


def align_series(s: pd.Series, alignment: str) -> pd.DataFrame:
    """Series (datetime index) → frame [year_key, axis_day, value], where
    year_key is the alignment-year label and axis_day is the 0-based position
    within the alignment year on a fixed non-leap calendar (29 Feb folds onto
    28 Feb) — so the same calendar date maps to the same axis_day every year,
    leap or not."""
    if s is None or s.empty:
        return pd.DataFrame(columns=["year_key", "axis_day", "value"])
    idx = pd.to_datetime(s.index)
    try:
        idx = idx.tz_localize(None)
    except TypeError:
        pass
    axis = [(_canonical_doy(t) - _canonical_doy(year_start(t, alignment))) % 365
            for t in idx]
    return pd.DataFrame({
        "year_key": [year_label(t, alignment) for t in idx],
        "axis_day": axis,
        "value": s.to_numpy(float),
    })


def segments(frame: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """[(year_key, sub-frame sorted by axis_day)] for non-empty years."""
    return [(k, g.sort_values("axis_day"))
            for k, g in frame.groupby("year_key") if not g.empty]


def daily_envelope(aligned: pd.DataFrame) -> pd.DataFrame:
    """Per-axis_day P10/P50/P90 across ALL years (the 'normal range').
    Days observed in fewer than 3 years are dropped (an envelope from one
    or two values isn't an envelope)."""
    if aligned.empty:
        return pd.DataFrame(columns=["axis_day", "p10", "p50", "p90"])
    g = aligned.groupby("axis_day")["value"]
    out = pd.DataFrame({
        "n": g.count(),
        "p10": g.quantile(0.10),
        "p50": g.quantile(0.50),
        "p90": g.quantile(0.90),
    }).reset_index()
    return out[out["n"] >= 3][["axis_day", "p10", "p50", "p90"]]


def available_years(aligned: pd.DataFrame, *, current: str) -> list[str]:
    """Past alignment-year labels (current year excluded), oldest first."""
    return sorted(k for k in aligned["year_key"].unique() if k != current)


def _ticks(alignment: str) -> tuple[list[int], list[str]]:
    return ((_WATER_TICKS, _WATER_LABELS) if alignment == WATER
            else (_CAL_TICKS, _CAL_LABELS))


def season_figure(shard: pd.Series, fan_median: pd.Series | None,
                  seasonal_median: pd.Series | None, *,
                  alignment: str, years: list[str],
                  show_envelope: bool = True,
                  today: pd.Timestamp | None = None) -> go.Figure:
    """The overlay figure. ``shard`` = observed daily GW (datetime index);
    ``fan_median``/``seasonal_median`` = forecast medians (datetime index,
    daily / monthly-midpoint). ``today`` anchors which alignment year is
    'current' (defaults to the shard's last date)."""
    import plotly.graph_objects as go

    fig = go.Figure()
    aligned = align_series(shard, alignment)
    anchor = pd.Timestamp(today) if today is not None else (
        pd.Timestamp(shard.index.max()) if shard is not None and not shard.empty
        else pd.Timestamp.now())
    current = year_label(anchor, alignment)

    if show_envelope and not aligned.empty:
        env = daily_envelope(aligned)
        if not env.empty:
            fig.add_trace(go.Scatter(x=env["axis_day"], y=env["p90"],
                                     mode="lines", line=dict(width=0),
                                     showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=env["axis_day"], y=env["p10"],
                                     mode="lines", line=dict(width=0),
                                     fill="tonexty", fillcolor=_ENVELOPE,
                                     name="historical P10–P90",
                                     hoverinfo="skip"))

    for key, g in segments(aligned):
        if key == current or key not in years:
            continue
        fig.add_trace(go.Scatter(
            x=g["axis_day"], y=g["value"], mode="lines", name=key,
            line=dict(color=_PAST, width=1.0),
            opacity=0.65,
            hovertemplate=key + " · %{y:.2f} mAOD<extra></extra>"))

    cur = aligned[aligned["year_key"] == current].sort_values("axis_day")
    if not cur.empty:
        fig.add_trace(go.Scatter(
            x=cur["axis_day"], y=cur["value"], mode="lines",
            name=f"{current} (observed)",
            line=dict(color=_NOW, width=2.2),
            hovertemplate="observed · %{y:.2f} mAOD<extra></extra>"))

    if fan_median is not None and not fan_median.empty:
        for key, g in segments(align_series(fan_median, alignment)):
            fig.add_trace(go.Scatter(
                x=g["axis_day"], y=g["value"], mode="lines",
                name=f"forecast ({key})",
                line=dict(color=_FORECAST, width=2.0),
                hovertemplate="forecast · %{y:.2f} mAOD<extra></extra>"))

    if seasonal_median is not None and not seasonal_median.empty:
        for key, g in segments(align_series(seasonal_median, alignment)):
            fig.add_trace(go.Scatter(
                x=g["axis_day"], y=g["value"], mode="lines+markers",
                name=f"seasonal ({key})",
                line=dict(color=_SEASONAL, width=1.5, dash="dot"),
                marker=dict(size=7, symbol="circle-open",
                            line=dict(width=1.5, color=_SEASONAL)),
                hovertemplate="seasonal monthly mean · %{y:.2f} mAOD"
                              "<extra></extra>"))

    ticks, labels = _ticks(alignment)
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(tickvals=ticks, ticktext=labels, range=[0, 366],
                   title=None, showgrid=True, gridcolor="#f0f0f0"),
        yaxis_title="GW level (mAOD)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, font=dict(size=10)),
        plot_bgcolor="white", paper_bgcolor="white",
        hovermode="x unified",
    )
    return fig


def render_season_view(sid: str, fsub: pd.DataFrame,
                       seasonal: pd.DataFrame | None, *,
                       key_prefix: str = "season") -> None:
    """Controls + figure (no-op-safe when the shard is missing)."""
    import streamlit as st

    from src.dashboard.loaders import load_gw_for_bh
    shard_df = load_gw_for_bh(sid)
    if shard_df is None or shard_df.empty:
        st.caption("Season view needs the per-station history shard — run "
                   "`python -m scripts.v15_build_per_station_parquet`.")
        return
    shard = pd.Series(shard_df["GW_Level"].to_numpy(float),
                      index=pd.to_datetime(shard_df["date"]))

    c1, c2 = st.columns([1, 2])
    alignment = c1.radio("Year alignment", ["Calendar", "Water year (Oct–Sep)"],
                         horizontal=True, key=f"{key_prefix}_align_{sid[:8]}",
                         label_visibility="collapsed")
    alignment = WATER if alignment.startswith("Water") else CALENDAR

    aligned = align_series(shard, alignment)
    current = year_label(shard.index.max(), alignment)
    past = available_years(aligned, current=current)
    default = past[-5:]
    years = c2.multiselect("Previous years", past, default=default,
                           key=f"{key_prefix}_years_{sid[:8]}",
                           label_visibility="collapsed",
                           placeholder="Previous years to overlay")

    fan_median = None
    if fsub is not None and not fsub.empty and "gw_p50" in fsub.columns:
        fan_median = pd.Series(fsub["gw_p50"].to_numpy(float),
                               index=pd.to_datetime(fsub["date"]))
    seasonal_median = None
    if (seasonal is not None and not seasonal.empty
            and {"month_start", "gw_p50"}.issubset(seasonal.columns)):
        s = seasonal.dropna(subset=["gw_p50"]).sort_values("month_start")
        seasonal_median = pd.Series(
            s["gw_p50"].to_numpy(float),
            index=pd.to_datetime(s["month_start"]) + pd.Timedelta(days=14))

    fig = season_figure(shard, fan_median, seasonal_median,
                        alignment=alignment, years=years,
                        today=shard.index.max())
    st.plotly_chart(fig, width="stretch", key=f"{key_prefix}_{sid[:8]}")
    st.caption("Grey = selected previous years · band = historical P10–P90 "
               "(all years) · black = this year observed · blue = forecast "
               "median · dotted circles = seasonal monthly means "
               "(experimental). Years aligned by "
               + ("water year (Oct–Sep)." if alignment == WATER
                  else "calendar year."))
