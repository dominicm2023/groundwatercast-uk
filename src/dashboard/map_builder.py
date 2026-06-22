"""
Folium map builder for the Groundwater Risk dashboard.

Key design decisions
--------------------
Station-click matching
    The station_id is embedded in the popup HTML via a ``data-sid`` attribute.
    ``st_folium`` returns the raw popup HTML in ``last_object_clicked_popup``,
    which the app parses with a single regex to get an exact ID match —
    no lat/lon arithmetic or nearest-neighbour lookup required.

Marker radius
    Class-stratified with sqrt-scaled fine adjustment within each class:
      LOW    base=5,  fine up to +4  → radius in [5,  9]
      MEDIUM base=10, fine up to +5  → radius in [10, 15]
      HIGH   base=16, fine up to +8  → radius in [16, 24]
    Using sqrt compression avoids a single extreme outlier dominating
    while preserving clear visual separation between classes.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import folium
import pandas as pd

# Tier→action vocabulary for marker popups (relocated from the retired
# risk-index data_loader; wording is forecast-flavoured, not operational —
# this is an indicative research tool, not a duty rota).
ACTION_LABEL: dict[str, str] = {
    "IMMEDIATE_ACTION": "Breach likely",
    "SUSTAINED_HIGH": "Sustained high",
    "EMERGING_RISK": "Worth watching",
    "STABLE_LOW": "Stable / low",
}

ACTION_ADVICE: dict[str, str] = {
    "IMMEDIATE_ACTION": "High breach probability — open the forecast detail",
    "SUSTAINED_HIGH": "Persistently elevated — review the seasonal outlook",
    "EMERGING_RISK": "Rising signal — check the fan and first-crossing dates",
    "STABLE_LOW": "No action suggested by the forecast",
}
from src.dashboard.geology import (
    AQUIFER_STYLE,
    aquifer_designations_present,
    load_aquifer_layer,
)

# ---------------------------------------------------------------------------
# Region boundary — loaded once and reused for every map. The polygon comes
# from config.region.geojson_path (the same one the catalogue build filters
# with), so the map overlay always matches the catalogue scope.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parents[2]
_REGION_FILL = "rgba(31, 119, 180, 0.10)"
_REGION_STROKE = "#1f77b4"


@lru_cache(maxsize=1)
def _region_cfg() -> tuple[Path, str]:
    """(geojson path, display name) from config.region; sensible fallback."""
    try:
        cfg = json.loads((_ROOT / "config" / "config.json").read_text())
        region = cfg.get("region", {})
        return _ROOT / region["geojson_path"], region.get("name", "Region")
    except (OSError, ValueError, KeyError):
        return _ROOT / "data" / "regions" / "england_wales.geojson", "England"


def _region_name() -> str:
    return _region_cfg()[1]


@lru_cache(maxsize=1)
def _load_region_geojson() -> dict | None:
    """
    Load the configured region GeoJSON once (cached).
    Returns ``None`` if the file is missing so the map still renders
    without the overlay (caller is expected to warn in the UI).
    """
    path = _region_cfg()[0]
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _region_bounds(geojson: dict) -> list[list[float]]:
    """
    Return ``[[lat_min, lon_min], [lat_max, lon_max]]`` for the region —
    the format folium's ``fit_bounds`` expects.
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    geoms = [shape(f["geometry"]) for f in geojson["features"]]
    merged = unary_union(geoms)
    lon_min, lat_min, lon_max, lat_max = merged.bounds
    return [[lat_min, lon_min], [lat_max, lon_max]]

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_RISK_FILL: dict[str, str] = {
    "LOW": "#2ca02c",
    "MEDIUM": "#ff7f0e",
    "HIGH": "#d62728",
}
_RISK_BORDER: dict[str, str] = {
    "LOW": "#1d7a20",
    "MEDIUM": "#cc6200",
    "HIGH": "#9a1010",
}
_ACTION_ACCENT: dict[str, str] = {
    "IMMEDIATE_ACTION": "#d62728",
    "SUSTAINED_HIGH": "#d46b08",
    "EMERGING_RISK": "#e08a00",
    "STABLE_LOW": "#2ca02c",
}

# ---------------------------------------------------------------------------
# Marker radius
# ---------------------------------------------------------------------------

# Base radius (px) per risk class — ensures LOW/MEDIUM/HIGH are always
# visually distinct even when scores overlap across classes.
_BASE_RADIUS: dict[str, float] = {"LOW": 5.0, "MEDIUM": 10.0, "HIGH": 16.0}

# Expected score range per class used to normalise the fine adjustment.
# Scores outside this range are clamped to [0, 1].
_SCORE_RANGE: dict[str, tuple[float, float]] = {
    "LOW": (-1.0, 0.5),
    "MEDIUM": (0.5, 1.5),
    "HIGH": (1.5, 3.0),
}

# Maximum additional radius added on top of the base (sqrt-scaled).
_FINE_MAX: dict[str, float] = {"LOW": 4.0, "MEDIUM": 5.0, "HIGH": 8.0}


def marker_radius(risk_score: float, risk_class: str) -> float:
    """
    Compute circle marker radius.

    Base radius is set by risk class so LOW/MEDIUM/HIGH never overlap
    visually.  A fine adjustment (sqrt-scaled within the class score range)
    adds up to ``_FINE_MAX[class]`` pixels, compressing the contribution of
    extreme outliers.

    Parameters
    ----------
    risk_score : Raw risk score (can be negative).
    risk_class : "LOW", "MEDIUM", or "HIGH".
    """
    base = _BASE_RADIUS.get(risk_class, 5.0)
    lo, hi = _SCORE_RANGE.get(risk_class, (0.0, 2.0))
    fine_max = _FINE_MAX.get(risk_class, 4.0)

    t = (risk_score - lo) / max(hi - lo, 1e-6)
    t = max(0.0, min(1.0, t))           # clamp to [0, 1]
    return base + fine_max * math.sqrt(t)


# ---------------------------------------------------------------------------
# Popup HTML
# ---------------------------------------------------------------------------

def _format_aquifer(row: pd.Series) -> str:
    """
    Compact one-liner used in tooltips and popups.

    Returns 'Chalk (Principal)' style when both name and designation are
    distinct, '—' when neither is present, otherwise whichever is set.
    """
    name = row.get("aquifer_name")
    des = row.get("aquifer_designation")
    name = None if (name is None or pd.isna(name)) else str(name).strip()
    des = None if (des is None or pd.isna(des)) else str(des).strip()
    if not name and not des:
        return "—"
    if name and des and des not in name:
        return f"{name} ({des})"
    return name or des  # type: ignore[return-value]


def _confidence_swatch(level: str) -> str:
    """Inline-styled span for the confidence label inside popups."""
    palette = {
        "HIGH":   ("#2ca02c", "Fresh"),
        "MEDIUM": ("#ff7f0e", "Recent"),
        "LOW":    ("#888888", "Stale"),
    }
    color, label = palette.get(level, ("#888888", "Unknown"))
    return (
        f'<span style="background:{color};color:white;'
        f'padding:1px 6px;border-radius:3px;font-weight:bold;'
        f'font-size:10px">{level} · {label}</span>'
    )


def _popup_html(row: pd.Series) -> str:
    """
    Styled HTML popup card.

    IMPORTANT: the ``data-sid`` attribute on the outer <div> carries the
    station_id.  The app extracts it with:
        re.search(r'data-sid="([^"]+)"', popup_html)
    This gives an exact match — no coordinate arithmetic needed.
    """
    rc = str(row["risk_raw"])
    fill = _RISK_FILL.get(rc, "#888")
    action_cat = str(row.get("action_category", "STABLE_LOW"))
    accent = _ACTION_ACCENT.get(action_cat, "#888")
    label = ACTION_LABEL.get(action_cat, action_cat)
    advice = ACTION_ADVICE.get(action_cat, "")
    name = str(row.get("station_name", str(row["station_id"])[:8]))
    reason = str(row.get("reason_text", "—"))
    trend = str(row.get("trend", "—"))
    score = float(row["risk_score"])
    days = int(row.get("persistence_days", 0))

    # Freshness fields (supplied by the page's map-snapshot adapter)
    age_days = row.get("data_age_days")
    confidence = str(row.get("confidence_level", "")).upper() or "HIGH"
    last_obs = row.get("dateTime")
    if isinstance(last_obs, pd.Timestamp):
        last_obs_str = last_obs.strftime("%d %b %Y")
    else:
        last_obs_str = str(last_obs) if last_obs is not None else "—"

    # Optional extrapolation + live fields
    is_extrap = bool(row.get("is_extrapolated", False))
    extrap_horizon = row.get("extrapolation_horizon_days")
    is_live = bool(row.get("is_live", False))
    live_age_min = row.get("live_age_minutes")

    age_str = "—" if age_days is None or pd.isna(age_days) else f"{int(age_days)} days"

    # Build live age label if applicable
    live_label = ""
    if is_live and live_age_min is not None and not pd.isna(live_age_min):
        m = int(live_age_min)
        if m < 60:
            live_label = f"{m} min ago"
        elif m < 1440:
            live_label = f"{m // 60}h {m % 60}m ago"
        else:
            live_label = f"{m // 1440}d ago"

    return (
        f'<div data-sid="{row["station_id"]}" '
        f'style="font-family:\'Segoe UI\',Arial,sans-serif;min-width:220px;max-width:280px;padding:4px 0">'

        # Station name
        f'<div style="font-size:14px;font-weight:700;margin-bottom:6px;color:#1a3a5c">'
        f'{name}</div>'

        # Freshness banner — prominent at the top.  When the row is live,
        # the green LIVE chip replaces the standard confidence swatch.
        f'<div style="margin-bottom:6px;padding:4px 7px;background:#f5f5f5;'
        f'border-radius:3px;font-size:11px;color:#333">'
        + (
            f'<div style="margin-bottom:3px">'
            f'<span style="background:#00b050;color:white;padding:1px 7px;'
            f'border-radius:3px;font-weight:bold;font-size:11px">'
            f'● LIVE</span>'
            f'  <span style="color:#666;margin-left:6px">{live_label}</span>'
            f'</div>'
            if is_live else ""
        ) +
        f'<div><strong>Latest data:</strong> {last_obs_str}</div>'
        f'<div><strong>Data age:</strong> {age_str}</div>'
        + (
            f'<div style="margin-top:3px"><strong>Confidence:</strong> '
            f'{_confidence_swatch(confidence)}</div>'
            if not is_live else ""
        ) +
        (
            f'<div style="margin-top:3px;color:#cc6200">'
            f'<strong>⚠ Projected:</strong> '
            f'{int(extrap_horizon) if pd.notna(extrap_horizon) else "—"} days '
            f'forward-rolled</div>'
            if is_extrap else ""
        ) +
        f'</div>'

        # Data table
        f'<table style="width:100%;font-size:12px;border-collapse:collapse">'
        f'<tr><td style="color:#666;padding:2px 6px 2px 0">Risk</td>'
        f'<td><span style="background:{fill};color:white;padding:1px 7px;'
        f'border-radius:3px;font-weight:bold;font-size:11px">{rc}</span></td></tr>'
        f'<tr><td style="color:#666;padding:2px 6px 2px 0">Score</td>'
        f'<td style="font-weight:600">{score:.3f}</td></tr>'
        f'<tr><td style="color:#666;padding:2px 6px 2px 0">Trend</td>'
        f'<td>{trend}</td></tr>'
        f'<tr><td style="color:#666;padding:2px 6px 2px 0">Days HIGH</td>'
        f'<td>{days}</td></tr>'
        f'<tr><td style="color:#666;padding:2px 6px 2px 0">Aquifer</td>'
        f'<td style="font-size:11px">{_format_aquifer(row)}</td></tr>'
        f'<tr><td style="color:#666;padding:2px 6px 2px 0;vertical-align:top">Reason</td>'
        f'<td style="font-size:11px">{reason}</td></tr>'
        f'</table>'

        # Action callout
        f'<div style="margin-top:8px;padding:5px 8px;'
        f'background:{accent}22;border-left:3px solid {accent};'
        f'font-size:11px;color:#333;border-radius:0 3px 3px 0">'
        f'<strong>{label}</strong><br>{advice}</div>'

        f'</div>'
    )


def _tooltip_text(row: pd.Series) -> str:
    """Multi-line hover tooltip (station + freshness + aquifer context)."""
    name = str(row.get("station_name", str(row["station_id"])[:8]))
    aquifer = _format_aquifer(row)
    age = row.get("data_age_days")
    age_str = ""
    if age is not None and not pd.isna(age):
        a = int(age)
        if a <= 7:
            age_str = f"  ·  {a}d (fresh)"
        elif a <= 30:
            age_str = f"  ·  {a}d (recent)"
        else:
            age_str = f"  ·  {a}d (stale)"

    base = f"{name}  ·  {row['risk_raw']}  ·  {float(row['risk_score']):.2f}{age_str}"
    if row.get("is_live"):
        base += "  [live]"
    elif row.get("is_extrapolated"):
        base += "  [projected]"
    if aquifer != "—":
        base += f"\nAquifer: {aquifer}"
    return base


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------

def _make_legend(
    include_region: bool = True,
    aquifer_designations: list[str] | None = None,
) -> folium.Element:
    """Inject a small fixed-position HTML legend into the map."""
    region_row = (
        f"""
      <div style="border-top:1px solid #eee;margin:6px 0 4px;padding-top:6px;
                  font-weight:700;color:#1a3a5c">Region</div>
      <div style="display:flex;align-items:center">
        <span style="background:rgba(31,119,180,0.20);
          border:1.5px solid #1f77b4;
          width:14px;height:10px;display:inline-block;margin-right:7px"></span>
        {_region_name()} region
      </div>
        """
        if include_region else ""
    )
    # Aquifer legend rows — only shown when the overlay is enabled and at
    # least one designation is present in the loaded layer.
    aquifer_rows = ""
    if aquifer_designations:
        rows = []
        for des in aquifer_designations:
            style = AQUIFER_STYLE.get(des)
            if not style:
                continue
            # Convert hex + opacity to an rgba swatch for the legend
            hex_ = style["fill"].lstrip("#")
            r, g, b = int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16)
            rgba = f"rgba({r},{g},{b},{style['opacity']})"
            rows.append(
                f'<div style="display:flex;align-items:center;margin-bottom:3px">'
                f'<span style="background:{rgba};border:1px solid #888;'
                f'width:14px;height:10px;display:inline-block;margin-right:7px">'
                f'</span>{style["label"]}</div>'
            )
        aquifer_rows = (
            '<div style="border-top:1px solid #eee;margin:6px 0 4px;'
            'padding-top:6px;font-weight:700;color:#1a3a5c">Aquifer</div>'
            + "".join(rows)
        )

    # Collapsible legend using the native HTML <details>/<summary>
    # element — no JS required, so we side-step folium's iframe
    # sandboxing and Streamlit's script-stripping behaviour entirely.
    # The clickable <summary> is the collapse handle; the disclosure
    # triangle is rotated via CSS for a custom look.
    html = f"""
    <style>
      .gwrm-legend {{
          position: fixed; bottom: 12px; left: 12px; z-index: 1000;
          background: white; border: 1px solid #ddd; border-radius: 6px;
          font-family: 'Segoe UI', Arial, sans-serif;
          font-size: 11.5px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.10);
          max-width: 200px;
      }}
      .gwrm-legend summary {{
          cursor: pointer;
          list-style: none;
          padding: 8px 12px;
          font-weight: 700;
          color: #1a3a5c;
          display: flex; align-items: center; justify-content: space-between;
          user-select: none;
      }}
      .gwrm-legend summary::-webkit-details-marker {{ display: none; }}
      .gwrm-legend summary::after {{
          content: "−";
          font-size: 16px; font-weight: 700; color: #888;
          margin-left: 10px;
      }}
      /* Collapsed state — show only the round ⓘ icon, no text. */
      .gwrm-legend:not([open]) {{
          padding: 0;
          min-width: 0;
          max-width: none;
          width: 32px; height: 32px;
          border-radius: 50%;
          border-color: #ddd;
      }}
      .gwrm-legend:not([open]) summary {{
          padding: 0;
          width: 100%; height: 100%;
          justify-content: center; align-items: center;
      }}
      .gwrm-legend:not([open]) .gwrm-title {{ display: none; }}
      .gwrm-legend:not([open]) summary::after {{
          content: "ⓘ";
          color: #1a3a5c;
          margin: 0;
          font-size: 16px;
      }}
      .gwrm-legend .gwrm-body {{
          padding: 0 12px 10px 12px;
          min-width: 150px;
      }}
    </style>
    <details class="gwrm-legend" open>
      <summary><span class="gwrm-title">Risk Level</span></summary>
      <div class="gwrm-body">
        <div style="display:flex;align-items:center;margin-bottom:4px">
          <span style="background:#d62728;border-radius:50%;
            width:14px;height:14px;display:inline-block;margin-right:7px"></span>HIGH
        </div>
        <div style="display:flex;align-items:center;margin-bottom:4px">
          <span style="background:#ff7f0e;border-radius:50%;
            width:10px;height:10px;display:inline-block;margin-right:7px"></span>MEDIUM
        </div>
        <div style="display:flex;align-items:center">
          <span style="background:#2ca02c;border-radius:50%;
            width:7px;height:7px;display:inline-block;margin-right:7px"></span>LOW
        </div>
        <div style="color:#888;font-size:10px;margin-top:6px">
          Circle size proportional to risk score within class
        </div>
        <div style="border-top:1px solid #eee;margin:6px 0 4px;padding-top:6px;
                    font-weight:700;color:#1a3a5c">Data freshness</div>
        <div style="display:flex;align-items:center;margin-bottom:3px">
          <span style="background:#888;border:2px solid #00b050;
            border-radius:50%;width:12px;height:12px;display:inline-block;
            margin-right:7px;box-sizing:border-box"></span>
          Live (≤15 min)
        </div>
        <div style="display:flex;align-items:center;margin-bottom:3px">
          <span style="background:#888;border-radius:50%;
            width:10px;height:10px;display:inline-block;margin-right:7px"></span>
          Fresh (filled)
        </div>
        <div style="display:flex;align-items:center;margin-bottom:3px">
          <span style="border:2px solid #888;border-radius:50%;
            width:10px;height:10px;display:inline-block;margin-right:7px"></span>
          Stale (hollow)
        </div>
        <div style="display:flex;align-items:center">
          <span style="border:2px dashed #333;background:rgba(136,136,136,0.6);
            border-radius:50%;width:10px;height:10px;display:inline-block;
            margin-right:7px"></span>
          Projected (forward-rolled)
        </div>
        {region_row}
        {aquifer_rows}
      </div>
    </details>
    """
    return folium.Element(html)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _aquifer_style_function(feature):
    """
    Folium ``style_function`` for the aquifer GeoJson layer.

    Defined at module level so it stays a regular function (lambdas with
    closures are not picklable, which used to break ``@st.cache_data``).
    """
    props = feature.get("properties") or {}
    des = props.get("aquifer_class") or props.get("aquifer_designation")
    style = AQUIFER_STYLE.get(des or "")
    fill = style["fill"] if style else "#cccccc"
    opacity = style["opacity"] if style else 0.15
    return {
        "fillColor": fill,
        "color": fill,
        "weight": 0.5,
        "fillOpacity": opacity,
    }


def build_map(
    snapshot: pd.DataFrame,
    show_aquifer: bool = False,
    max_data_age_days: int | None = None,
) -> folium.Map:
    """
    Build and return a Folium Map populated with one CircleMarker per station,
    overlaid on the configured region boundary and (optionally) the
    EA Aquifer Designation layer.

    Each marker's popup HTML carries ``data-sid="<station_id>"`` so the app
    can extract the exact station ID from ``st_folium``'s
    ``last_object_clicked_popup`` return value.

    Parameters
    ----------
    snapshot     : One-row-per-station dataframe to plot.
    show_aquifer : When True, render the EA Aquifer Designation layer
                   beneath the markers (default off — busy layer).

    Render order
    ------------
    1. Tile base layer (CartoDB positron)
    2. Region polygon (filled, faint blue)
    3. EA Aquifer Designation layer (optional, beneath markers)
    4. Station CircleMarkers (one per row, always on top)
    5. Legend overlay (fixed-position HTML)
    """
    m = folium.Map(
        location=[51.1, -1.3],
        zoom_start=8,
        tiles="CartoDB positron",
        prefer_canvas=True,
    )

    # ── 1. Region overlay (must precede markers) ────────────────────────────
    region_geojson = _load_region_geojson()
    region_loaded = region_geojson is not None
    if region_loaded:
        folium.GeoJson(
            region_geojson,
            name=f"{_region_name()} region",
            style_function=lambda _f: {
                "fillColor": _REGION_FILL,
                "color": _REGION_STROKE,
                "weight": 2,
                "fillOpacity": 1.0,   # alpha already baked into _REGION_FILL
            },
            tooltip=folium.Tooltip(f"{_region_name()} region", sticky=False),
            highlight_function=lambda _f: {"weight": 3, "color": _REGION_STROKE},
            interactive=False,        # don't intercept marker clicks
        ).add_to(m)

        # Fit bounds so the map always opens framed on the region
        try:
            m.fit_bounds(_region_bounds(region_geojson))
        except Exception:
            # fit_bounds is non-critical; ignore any geometry edge cases
            pass

    # ── 2. Aquifer overlay (optional, beneath markers) ──────────────────────
    aquifer_designations: list[str] = []
    if show_aquifer:
        aquifer_geojson = load_aquifer_layer()
        if aquifer_geojson is not None:
            folium.GeoJson(
                aquifer_geojson,
                name="Indicative aquifer (BGS 625k, OGL)",
                style_function=_aquifer_style_function,
                tooltip=folium.GeoJsonTooltip(
                    fields=["aquifer_class"],
                    aliases=["Aquifer class:"],
                    sticky=False,
                ),
                interactive=False,    # don't intercept marker clicks
                smooth_factor=2.0,    # extra client-side simplification
            ).add_to(m)
            aquifer_designations = aquifer_designations_present(aquifer_geojson)

    # ── 3. Station markers (rendered above all polygon layers) ──────────────
    # Marker styling cascade:
    #   live          → solid risk colour + bright green outer ring
    #   stale         → hollow ring (risk-coloured border, no fill)
    #   extrapolated  → solid muted fill + dashed border
    #   default       → solid filled circle
    for _, row in snapshot.iterrows():
        rc = str(row["risk_raw"])
        radius = marker_radius(float(row["risk_score"]), rc)

        age = row.get("data_age_days")
        is_extrapolated = bool(row.get("is_extrapolated", False))
        is_live = bool(row.get("is_live", False))
        is_stale = (
            max_data_age_days is not None
            and age is not None
            and not pd.isna(age)
            and int(age) > int(max_data_age_days)
        )

        # Default: solid filled circle
        marker_kwargs = dict(
            radius=radius,
            color=_RISK_BORDER.get(rc, "#555"),
            weight=1.5,
            fill=True,
            fill_color=_RISK_FILL.get(rc, "#888"),
            fill_opacity=0.85,
        )

        # Live takes precedence over stale/extrapolated — the data IS
        # fresh, so the slider-based stale styling shouldn't apply.
        if is_live:
            marker_kwargs.update(
                color="#00b050",        # bright green outer ring
                weight=3.0,
                fill=True,
                fill_color=_RISK_FILL.get(rc, "#888"),
                fill_opacity=0.85,
            )
        elif is_stale:
            # Hollow ring — coloured border, no fill. Same risk colour.
            marker_kwargs.update(
                color=_RISK_FILL.get(rc, "#888"),
                weight=2.5,
                fill=False,
                fill_opacity=0.0,
            )
        elif is_extrapolated:
            # Forward-rolled prediction — solid but with a thicker dashed-style
            # border to flag it as projected (Folium doesn't support fill
            # patterns directly; thicker border + slightly reduced fill opacity
            # signals "not directly observed" without losing class colour).
            marker_kwargs.update(
                color="#333333",
                weight=2.5,
                fill_opacity=0.65,
                dash_array="4,3",
            )

        folium.CircleMarker(
            location=[float(row["lat"]), float(row["lon"])],
            tooltip=folium.Tooltip(_tooltip_text(row), sticky=False),
            popup=folium.Popup(_popup_html(row), max_width=300),
            **marker_kwargs,
        ).add_to(m)

    # ── 4. Legend ───────────────────────────────────────────────────────────
    m.get_root().html.add_child(_make_legend(
        include_region=region_loaded,
        aquifer_designations=aquifer_designations,
    ))
    return m
