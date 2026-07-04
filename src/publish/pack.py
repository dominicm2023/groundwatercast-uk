"""Artifact-pack builder — assembles the versioned static pack
(``outputs/pack/``) from existing pipeline artefacts.

The pack is the product's **public API**: the files a static front-end (the
MapLibre explorer) or any third party fetches over HTTP. Schema and
conventions are pinned in ``src/publish/contract.py`` and documented in
``docs/artifact_contract.md`` — those three must move together.

Design rules:
  - pure-read: no network, nothing outside ``out_dir`` is written;
  - one shard read per station, feeding both the current status and the
    observed tail (``status.status_from_series``);
  - wipe-and-rebuild via a ``<out_dir>.building`` swap — no stale
    ``stations/<id>.json`` survivors from a previous, larger scope;
  - injected ``now`` ⇒ byte-identical reruns (tested);
  - missing catalogue / empty shard dir → raise (a pack without stations is
    meaningless); missing forecast / seasonal / normals / freshness inputs →
    the corresponding fields are null and ``meta.inputs.<name>.status``
    records ``"missing"`` — the build succeeds loudly, never silently.

Streamlit-free; main env.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.publish.contract import (
    FAN_KEY_MAP,
    LEVEL_DP,
    NORMALS_ROW_KEYS,
    PCT_DP,
    PROB_DP,
    SCHEMA_VERSION,
    SEASONAL_MONTH_KEYS,
    SUMMARY_COL_SOURCES,
    TREND_FLAG_KEYS,
)
from src.dashboard.status import status_from_series
from src.forecast.live_levels import LIVE_STUCK_SOURCE

ATTRIBUTION = ("Contains Environment Agency data licensed under the Open "
               "Government Licence v3.0. Forecast forcing from ECMWF Open "
               "Data (CC-BY-4.0). Contains modified Copernicus Climate Change "
               "Service information (ERA5 reanalysis and SEAS5 seasonal "
               "forecasts). Bedrock geology from the British Geological "
               "Survey (BGS 625k, Open Government Licence v3.0).")
DISCLAIMER = ("Indicative, uncalibrated research forecast built on open "
              "data. Not flood warnings; not for safety-critical use. See "
              "the official Environment Agency flood warning service for "
              "operational decisions.")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PackInputs:
    """Everything ``build_pack`` consumes — frames already loaded so tests
    can construct synthetic packs without touching real paths."""
    catalogue: pd.DataFrame                  # required (GW rows)
    shard_dir: Path                          # required (per-station parquet)
    freshness: pd.DataFrame | None = None
    normals: pd.DataFrame | None = None
    pastas_summary: pd.DataFrame | None = None
    pastas_fan: pd.DataFrame | None = None
    seasonal: pd.DataFrame | None = None
    trend_flags: pd.DataFrame | None = None
    excluded_ids: frozenset[str] = frozenset()
    pinned_ids: frozenset[str] = frozenset()
    live_capable: int | None = None          # network count w/ an EA live feed
    region_name: str = ""
    source_meta: dict = field(default_factory=dict)
    geology_path: Path | None = None         # optional aquifer GeoJSON to ship


def _source_entry(path: Path, root: Path | None = None, *,
                  required: bool = False) -> tuple[pd.DataFrame | None, dict]:
    """(frame-or-None, provenance dict) for one CSV input. The recorded path
    is repo-relative — never the absolute local path (the pack is public)."""
    rel = (path.relative_to(root) if root else path).as_posix()
    if not path.exists():
        if required:
            raise FileNotFoundError(f"required pack input missing: {path}")
        return None, {"path": rel, "mtime_utc": None, "status": "missing"}
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (pd.read_csv(path),
            {"path": rel, "mtime_utc": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "status": "ok"})


def load_inputs(cfg: dict, root: Path) -> PackInputs:
    """Resolve every real input path. The only function here that knows the
    repo layout; ``build_pack`` itself is path-agnostic."""
    from src.dashboard.exclusions import excluded_station_ids
    from src.forecast.ensemble.thresholds import user_threshold_station_ids
    from src.forecast.ensemble.scope import live_capable_ids

    meta: dict = {}
    catalogue, meta["catalogue"] = _source_entry(
        root / "data/processed/catalogue.csv", root, required=True)
    freshness, meta["freshness"] = _source_entry(
        root / "data/processed/gw_freshness.csv", root)
    normals, meta["normals"] = _source_entry(
        root / "data/model/gw_monthly_normals.csv", root)
    summary, meta["pastas_summary"] = _source_entry(
        root / "data/model/forecast_pastas_summary.csv", root)
    fan, meta["pastas_fan"] = _source_entry(
        root / "data/model/forecast_pastas_fan.csv", root)
    seasonal, meta["seasonal"] = _source_entry(
        root / "data/model/forecast_seasonal_summary.csv", root)
    trend_flags, meta["trend_flags"] = _source_entry(
        root / "outputs/trend_flags.csv", root)

    shard_dir = root / "data/features/gw_by_station"
    if not shard_dir.exists() or not any(shard_dir.glob("*.parquet")):
        raise FileNotFoundError(
            f"no per-station shards under {shard_dir} — run "
            "`python -m scripts.run_chain --core` first.")

    # Optional bedrock-geology context layer (a lazy, off-by-default layer in
    # the explorer) — BGS Geology 625k, OGL/commercial-clean, classified to
    # indicative aquifer potential by scripts/build_bedrock_geology.py. Copied
    # verbatim; recorded but never required.
    geology = root / "data/geology/bedrock_625k.geojson"
    if geology.exists():
        mtime = datetime.fromtimestamp(geology.stat().st_mtime, tz=timezone.utc)
        meta["geology"] = {"path": geology.relative_to(root).as_posix(),
                           "mtime_utc": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "status": "ok"}
    else:
        geology = None
        meta["geology"] = {"path": "data/geology/bedrock_625k.geojson",
                           "mtime_utc": None, "status": "missing"}

    return PackInputs(
        catalogue=catalogue, shard_dir=shard_dir, freshness=freshness,
        normals=normals, pastas_summary=summary, pastas_fan=fan,
        seasonal=seasonal, trend_flags=trend_flags,
        excluded_ids=frozenset(excluded_station_ids()),
        pinned_ids=frozenset(user_threshold_station_ids()),
        live_capable=len(live_capable_ids()),
        region_name=cfg.get("region", {}).get("name", ""),
        source_meta=meta, geology_path=geology)


# ---------------------------------------------------------------------------
# JSON-safe primitives (the conventions of the contract)
# ---------------------------------------------------------------------------

def jround(x, dp: int) -> float | None:
    """Round for publication; NaN/inf/None → JSON null."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return round(v, dp)


def jint(x) -> int | None:
    v = jround(x, 0)
    return None if v is None else int(v)


def jbool(x) -> bool:
    """NaN-safe truthiness: bool(float('nan')) is True (NaN is truthy), so a
    missing/blank cell from a pandas row would publish as ``true``. Same defect
    class as the fan ``segment`` NaN — coerce through pd.isna first."""
    try:
        if pd.isna(x):
            return False
    except (TypeError, ValueError):
        pass
    return bool(x)


def iso_date(x) -> str | None:
    """YYYY-MM-DD or null."""
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    ts = pd.Timestamp(x)
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def iso_utc(x) -> str | None:
    """ISO-8601 UTC with Z suffix, or null."""
    if x is None:
        return None
    ts = pd.Timestamp(x)
    if pd.isna(ts):
        return None
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _str_or_none(x) -> str | None:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    s = str(x)
    return s if s and s.lower() != "nan" else None


def _dump(obj, fp: Path, *, pretty: bool) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, allow_nan=False, ensure_ascii=False,
                  indent=2 if pretty else None,
                  separators=None if pretty else (",", ":"))


# ---------------------------------------------------------------------------
# Per-station shard read (mirrors seeding.freshest_gw, with shard_dir injected)
# ---------------------------------------------------------------------------

def _read_shard(shard_dir: Path, sid: str) -> pd.Series:
    fp = shard_dir / f"{sid}.parquet"
    if not fp.exists():
        return pd.Series(dtype="float64", name="GW_Level")
    # Read data_source where the shard carries it so a frozen-telemetry row
    # (LIVE_STUCK_SOURCE) is excluded from the status/observed series — a stuck
    # value must not paint a confident, fresh current status on the map. Older
    # full-rebuild shards may lack the column; fall back to the bare read.
    try:
        df = pd.read_parquet(fp, columns=["date", "GW_Level", "data_source"])
        df = df[df["data_source"].astype(str) != LIVE_STUCK_SOURCE]
    except (ValueError, KeyError):
        df = pd.read_parquet(fp, columns=["date", "GW_Level"])
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s = pd.Series(df["GW_Level"].to_numpy(float), index=idx.normalize(),
                  name="GW_Level").dropna()
    return s.sort_index()


# ---------------------------------------------------------------------------
# Block builders (pure)
# ---------------------------------------------------------------------------

def _status_block(st: dict) -> dict:
    return {
        "status": st["status"],
        "percentile": jround(st["percentile"], PCT_DP),
        "trend": st["trend"],
        "level": jround(st["level"], LEVEL_DP),
        "obs_date": iso_date(st["obs_date"]),
        "obs_age_days": jint(st["age_days"]),
        "sgi": jround(st["sgi"], 2),
    }


def _freshness_block(row: pd.Series | None) -> dict:
    if row is None:
        return {"label": None, "days_since": None,
                "last_real_reading": None, "data_source": None}
    return {
        "label": _str_or_none(row.get("freshness_label")),
        "days_since": jint(row.get("days_since")),
        "last_real_reading": iso_date(row.get("last_real_reading")),
        "data_source": _str_or_none(row.get("data_source")),
    }


def _forecast_block(srow: pd.Series, fan: pd.DataFrame | None) -> dict:
    out: dict = {}
    for key, col in SUMMARY_COL_SOURCES.items():
        val = srow.get(col)
        if key == "run":
            out[key] = iso_utc(val)
        elif key in ("origin_date", "first_cross_median",
                     "first_cross_p25", "first_cross_p75"):
            out[key] = iso_date(val)
        elif key in ("threshold", "gw_p50_end", "model_spread_mean"):
            out[key] = jround(val, LEVEL_DP)
        elif key in ("p_breach", "p_breach_14d", "p_above_p90_14d",
                     "censored_frac"):
            out[key] = jround(val, PROB_DP)
        elif key in ("stale_days", "horizon_days", "first_cross_median_lead",
                     "n_members", "n_samples"):
            out[key] = jint(val)
        else:                                   # threshold_source, headline
            out[key] = _str_or_none(val)
    out["tier"] = _str_or_none(srow.get("tier"))
    out["is_pinned"] = jbool(srow.get("is_pinned", False))

    rows = []
    if fan is not None and not fan.empty:
        for _, r in fan.sort_values("lead").iterrows():
            # NaN is truthy, so `r.get("segment") or "forecast"` would pass a
            # present-but-NaN cell through to null — resolve NaN/None FIRST.
            entry = {"lead": jint(r["lead"]), "date": iso_date(r["date"]),
                     "segment": _str_or_none(r.get("segment")) or "forecast"}
            for src, dst in FAN_KEY_MAP.items():
                dp = PROB_DP if dst.startswith("p_") else LEVEL_DP
                entry[dst] = jround(r.get(src), dp)
            rows.append(entry)
    out["fan"] = rows
    return out


def _seasonal_block(srows: pd.DataFrame) -> dict | None:
    if srows is None or srows.empty:
        return None
    srows = srows.sort_values("month_ahead")
    first = srows.iloc[0]
    months = []
    for _, r in srows.iterrows():
        months.append({
            "month_ahead": jint(r["month_ahead"]),
            "month_start": iso_date(r["month_start"]),
            "p_below": jround(r["p_below"], PROB_DP),
            "p_near": jround(r["p_near"], PROB_DP),
            "p_above": jround(r["p_above"], PROB_DP),
            "gw_p10": jround(r["gw_p10"], LEVEL_DP),
            "gw_p50": jround(r["gw_p50"], LEVEL_DP),
            "gw_p90": jround(r["gw_p90"], LEVEL_DP),
        })
    return {
        "run": iso_utc(first.get("run")),
        "origin_date": iso_date(first.get("origin_date")),
        "seas5_weighted": jbool(first.get("seas5_weighted", False)),
        "n_traces": jint(first.get("n_traces")),
        "months": months,
    }


def _normals_rows(nrows: pd.DataFrame | None) -> list[dict]:
    if nrows is None or nrows.empty:
        return []
    out = []
    for _, r in nrows.sort_values("month").iterrows():
        out.append({k: (jint(r[k]) if k in ("month", "n_years")
                        else jround(r[k], LEVEL_DP))
                    for k in NORMALS_ROW_KEYS})
    return out


def _trend_flag_block(row: pd.Series | None) -> dict | None:
    """The borehole's row from outputs/trend_flags.csv → the published
    ``trend_flag`` block, or None when unflagged. Report-only non-stationarity
    signal (roadmap 1.1): the verdict + the signals behind it."""
    if row is None:
        return None
    return {
        "severity": _str_or_none(row.get("severity")),
        "provenance_class": _str_or_none(row.get("provenance_class")),
        "recommended_action": _str_or_none(row.get("recommended_action")),
        "slope_sen_m_yr": jround(row.get("slope_sen_m_yr"), LEVEL_DP),
        "trend_change_m": jround(row.get("trend_change_m"), LEVEL_DP),
        "rain_corr": jround(row.get("rain_corr"), 2),
        "isolation_class": _str_or_none(row.get("isolation_class")),
        "neighbour_count": jint(row.get("neighbour_count")),
        "already_in_register": jbool(row.get("already_in_register", False)),
    }


# ---------------------------------------------------------------------------
# Forecast timeline (map scrubber). A compact per-borehole status + opacity
# sequence so the explorer can recolour the map by scrubbing through the
# forecast: Today -> +1 wk -> +2 wk (daily fan: P50 vs that month's normals)
# -> Months 1..6 (most-likely seasonal tercile). Colour = most-likely category;
# opacity = confidence x a gentle lead-time fade, so a future frame never reads
# as a confident prediction. Boreholes with no forecast for a frame report
# "none" (faint grey) -- honest about where there is no signal.
# ---------------------------------------------------------------------------
TIMELINE_FAN_LEADS = (7, 14)
TIMELINE_SEASONAL_MONTHS = 6
TIMELINE_FRAMES: tuple[tuple[str, str], ...] = (
    ("Today", "now"), ("+1 wk", "fan"), ("+2 wk", "fan"),
    *tuple((f"Month {m}", "seasonal")
           for m in range(1, TIMELINE_SEASONAL_MONTHS + 1)),
)
_OP_MIN, _OP_MAX = 0.18, 0.9
# Frame-0 ("Today") opacities: solid for a measured status, faint for an
# "estimated" dot (the nowcast / modelled-today status when the latest reading
# is stale), and muted grey when there is neither.
_OP_OBSERVED, _OP_ESTIMATED, _OP_NO_DATA = 0.92, 0.45, 0.55


def _seasonal_month_starts(seasonal_all: pd.DataFrame | None) -> list:
    """The run's seasonal month_start per month_ahead (1..N), for real-days
    frame spacing. Restricted to the DOMINANT origin_date cohort: an archive
    predating the fleet-uniform anchoring carries per-borehole origins, and a
    naive first-row-per-month pick can land on a months-stale borehole whose
    "Month 1" started last spring. [] when there's no seasonal data."""
    if seasonal_all is None or seasonal_all.empty \
            or "month_start" not in seasonal_all.columns:
        return []
    df = seasonal_all.dropna(subset=["month_start"])
    if df.empty:
        return []
    if "origin_date" in df.columns and df["origin_date"].notna().any():
        dominant = df["origin_date"].mode()
        if len(dominant):
            df = df[df["origin_date"] == dominant.iloc[0]]
    firsts = df.drop_duplicates("month_ahead").sort_values("month_ahead")
    return list(firsts["month_start"])


def _frame_days(seasonal_month_starts: list | None = None,
                now: pd.Timestamp | None = None) -> list[int]:
    """Day-offset of each frame so the explorer can space the scrubber by real
    elapsed time (weekly steps near, monthly steps far) rather than evenly.

    The seasonal months are anchored AFTER the fan terminal (~2 weeks out), so
    "Month 1" genuinely starts ~1.5-2.5 months ahead — when the run's actual
    ``month_start`` dates are provided, each seasonal frame is placed at its
    month MID-point in real days-ahead (the old 30*mi approximation put Month 1
    at day 30 while its valid period began ~2 months out). Falls back to the
    approximation when no seasonal data exists."""
    starts = list(seasonal_month_starts or [])
    now = pd.Timestamp(now if now is not None else pd.Timestamp.utcnow()) \
        .tz_localize(None).normalize()
    days, fi, mi = [], 0, 0
    for _label, kind in TIMELINE_FRAMES:
        if kind == "fan":
            days.append(int(TIMELINE_FAN_LEADS[fi]))
            fi += 1
        elif kind == "seasonal":
            off = None
            if mi < len(starts) and starts[mi] is not None:
                ms = pd.Timestamp(starts[mi])
                ms = ms.tz_localize(None) if ms.tzinfo else ms
                off = int((ms.normalize() - now).days + 14)   # month mid-point
            # a stale cohort can compute an offset inside (or before) the fan
            # window — fall back to the approximation rather than mislead
            usable = off is not None and off > int(TIMELINE_FAN_LEADS[-1])
            days.append(off if usable else 30 * (mi + 1))
            mi += 1
        else:  # now
            days.append(0)
    # monotonic guard: a stale seasonal run must never place a month BEFORE the fan
    for i in range(1, len(days)):
        if days[i] <= days[i - 1]:
            days[i] = days[i - 1] + 1
    return days


def _frame_opacity(conf: float, frame_idx: int) -> float:
    fade = max(0.45, 1.0 - 0.07 * frame_idx)
    c = max(0.0, min(1.0, conf))
    return round(_OP_MIN + (_OP_MAX - _OP_MIN) * c * fade, 3)


def _cat_of(level, qrow) -> str | None:
    """below / near / above of a level vs a normals row (t1/t2), or None."""
    if level is None or pd.isna(level) or qrow is None:
        return None
    lv = float(level)
    if lv < float(qrow["t1"]):
        return "below"
    if lv > float(qrow["t2"]):
        return "above"
    return "near"


def _fan_frame(fan: pd.DataFrame, lead: int, month_norms: dict) -> tuple[str, float]:
    """Category (of P50) + confidence (P10/P50/P90 agreement) at a fan lead."""
    pos = int((fan["lead"] - lead).abs().to_numpy().argmin())
    f = fan.iloc[pos]
    try:
        month = pd.Timestamp(f["date"]).month
    except Exception:
        return "none", 0.0
    qrow = month_norms.get(int(month))
    p50 = _cat_of(f.get("gw_p50"), qrow)
    if p50 is None:
        return "none", 0.0
    cats = {c for c in (_cat_of(f.get("gw_p10"), qrow), p50,
                        _cat_of(f.get("gw_p90"), qrow)) if c is not None}
    conf = 1.0 if len(cats) <= 1 else (0.6 if len(cats) == 2 else 0.35)
    return p50, conf


def _seasonal_frame(month: dict) -> tuple[str, float]:
    """Most-likely tercile + confidence (how decisive the winning probability)."""
    probs = {"below": month.get("p_below"), "near": month.get("p_near"),
             "above": month.get("p_above")}
    probs = {k: float(v) for k, v in probs.items() if v is not None and not pd.isna(v)}
    if not probs:
        return "none", 0.0
    cat = max(probs, key=probs.get)
    conf = max(0.0, min(1.0, (probs[cat] - 1 / 3) / (2 / 3)))
    return cat, conf


def status_timeline(current_status: str | None, fan: pd.DataFrame | None,
                    seasonal_months: list[dict] | None,
                    month_norms: dict) -> tuple[list[str], list[float]]:
    """Per-frame (category, opacity) arrays over TIMELINE_FRAMES for one borehole.

    Frame 0 ("Today") shows the measured current status (solid) where the latest
    reading is fresh; if it is stale but a forecast exists, it falls back to the
    nowcast (modelled-today) status rendered faint = "estimated"; otherwise a
    muted grey. Later frames are the fan / seasonal outlook (opacity = confidence
    x lead-time fade)."""
    st_seq: list[str] = []
    op_seq: list[float] = []
    have_fan = fan is not None and not fan.empty
    fan_i = 0
    for i, (_label, kind) in enumerate(TIMELINE_FRAMES):
        if kind == "now":
            if current_status:
                cat, op = current_status, _OP_OBSERVED           # measured, fresh
            else:
                nc = _fan_frame(fan, 0, month_norms)[0] if have_fan else "none"
                cat, op = ((nc, _OP_ESTIMATED) if nc != "none"   # modelled today
                           else ("none", _OP_NO_DATA))           # no signal
        elif kind == "fan":
            cat, conf = (_fan_frame(fan, TIMELINE_FAN_LEADS[fan_i], month_norms)
                         if have_fan else ("none", 0.0))
            op = _frame_opacity(conf, i)
            fan_i += 1
        else:  # seasonal
            m_idx = i - 1 - len(TIMELINE_FAN_LEADS)
            cat, conf = (_seasonal_frame(seasonal_months[m_idx])
                         if (seasonal_months and 0 <= m_idx < len(seasonal_months))
                         else ("none", 0.0))
            op = _frame_opacity(conf, i)
        st_seq.append(cat)
        op_seq.append(round(op, 3))
    return st_seq, op_seq


def station_feature(cat_row: pd.Series, status: dict, fresh: dict,
                    fcst: dict | None, has_seasonal: bool,
                    trend_flag: dict | None = None,
                    status_seq: list | None = None,
                    opacity_seq: list | None = None,
                    slug: str | None = None) -> dict:
    """One GeoJSON Feature — flat properties for MapLibre data-driven styling.
    (Document promoteId: "station_id" for feature-state on the consumer side.)"""
    props = {
        "station_id": str(cat_row["station_id"]),
        "slug": slug,
        "name": _str_or_none(cat_row.get("station_name")),
        "aquifer": _str_or_none(cat_row.get("aquifer_name")),
        "aquifer_designation": _str_or_none(cat_row.get("aquifer_designation")),
        **status,
        "freshness": fresh["label"],
        "days_since": fresh["days_since"],
        "data_source": fresh["data_source"],
        "tier": fcst["tier"] if fcst else None,
        "p_breach_14d": fcst["p_breach_14d"] if fcst else None,
        "p_above_p90_14d": fcst["p_above_p90_14d"] if fcst else None,
        "first_cross_median": fcst["first_cross_median"] if fcst else None,
        "headline": fcst["headline"] if fcst else None,
        "threshold": fcst["threshold"] if fcst else None,
        "threshold_source": fcst["threshold_source"] if fcst else None,
        "is_pinned": fcst["is_pinned"] if fcst else False,
        "has_forecast": fcst is not None,
        "has_seasonal": has_seasonal,
        "has_trend_flag": trend_flag is not None,
        "trend_severity": trend_flag["severity"] if trend_flag else None,
        "st_seq": status_seq if status_seq is not None else [],
        "op_seq": opacity_seq if opacity_seq is not None else [],
    }
    return {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [jround(cat_row["lon"], 6),
                                     jround(cat_row["lat"], 6)]},
        "properties": props,
    }


def station_detail(cat_row: pd.Series, status: dict, fresh: dict,
                   normals_rows: list[dict], observed: list,
                   fcst: dict | None, seasonal: dict | None,
                   status_month: int | None,
                   trend_flag: dict | None = None,
                   slug: str | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "station": {
            "station_id": str(cat_row["station_id"]),
            "slug": slug,
            "name": _str_or_none(cat_row.get("station_name")),
            "lat": jround(cat_row["lat"], 6),
            "lon": jround(cat_row["lon"], 6),
            "aquifer": _str_or_none(cat_row.get("aquifer_name")),
            "aquifer_designation": _str_or_none(cat_row.get("aquifer_designation")),
        },
        "status": {**status, "month": status_month},
        "freshness": fresh,
        "normals": normals_rows,
        "observed": {"unit": "mAOD", "series": observed},
        "forecast": fcst,
        "seasonal": seasonal,
        "trend_flag": trend_flag,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _latest_run_only(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Keep only the most recent run when an artefact carries several."""
    if df is None or df.empty or "run" not in df.columns:
        return df
    return df[df["run"] == df["run"].max()]


def build_manifest(out_dir: Path) -> dict:
    files = {}
    for fp in sorted(out_dir.rglob("*")):
        if not fp.is_file() or fp.name == "manifest.json":
            continue
        rel = fp.relative_to(out_dir).as_posix()
        files[rel] = {"sha256": hashlib.sha256(fp.read_bytes()).hexdigest(),
                      "bytes": fp.stat().st_size}
    return {"schema_version": SCHEMA_VERSION, "files": files}


def build_pack(inputs: PackInputs, out_dir: Path, *,
               now: pd.Timestamp | str | None = None,
               history_days: int = 1100,
               include_history_for: str = "all",
               public_trend_provenance: frozenset[str] = frozenset({"artifact_like"}),
               pretty: bool = False) -> dict:
    """Build the full pack under ``out_dir`` (atomically, via a ``.building``
    swap). Returns the meta dict. Raises on a meaningless pack (no catalogue
    rows / no shards); degrades to nulls on missing optional inputs.

    ``public_trend_provenance`` is the set of trend-screen provenance classes
    surfaced on the public map (the explorer badge). The screen is an operator
    review queue; only high-confidence data-quality classes (default
    ``artifact_like``) belong on the public map — the rest of trend_flags.csv
    (real multi-year trends, transient step_shifts) stays operator-only."""
    now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    now_ts = now_ts.tz_localize("UTC") if now_ts.tzinfo is None else now_ts.tz_convert("UTC")

    cat = inputs.catalogue
    if "measure_type" in cat.columns:
        cat = cat[cat["measure_type"] == "groundwater"]
    cat = cat.drop_duplicates("station_id")
    cat = cat[cat["lat"].notna() & cat["lon"].notna()]
    if cat.empty:
        raise ValueError("catalogue has no groundwater stations with coordinates")

    excluded = {sid for sid in cat["station_id"].astype(str)
                if sid in inputs.excluded_ids}
    cat = cat[~cat["station_id"].astype(str).isin(excluded)]

    # Triage gives tier + is_pinned per forecast station (reused, not rebuilt).
    summary = _latest_run_only(inputs.pastas_summary)
    fan_all = _latest_run_only(inputs.pastas_fan)
    seasonal_all = _latest_run_only(inputs.seasonal)
    triage_by_sid: dict[str, pd.Series] = {}
    if summary is not None and not summary.empty:
        from src.dashboard.forecast_outlook import build_pastas_triage
        triage = build_pastas_triage(summary, cat, set(inputs.pinned_ids))
        triage_by_sid = {str(r["station_id"]): r for _, r in triage.iterrows()}

    fresh_by_sid: dict[str, pd.Series] = {}
    if inputs.freshness is not None and not inputs.freshness.empty:
        fresh_by_sid = {str(r["station_id"]): r
                        for _, r in inputs.freshness.iterrows()}

    # Trend-screen stability flags (roadmap 1.1) — an operator review queue.
    # Only high-confidence data-quality classes (public_trend_provenance,
    # default artifact_like) surface on the public map; the rest of
    # trend_flags.csv (real trends, transient step_shifts) stays operator-only.
    trend_by_sid: dict[str, pd.Series] = {}
    if inputs.trend_flags is not None and not inputs.trend_flags.empty:
        trend_by_sid = {
            str(r["station_id"]): r
            for _, r in inputs.trend_flags.iterrows()
            if str(r.get("provenance_class")) in public_trend_provenance
        }

    normals = inputs.normals if inputs.normals is not None else pd.DataFrame()

    building = out_dir.parent / (out_dir.name + ".building")
    if building.exists():
        shutil.rmtree(building)
    (building / "stations").mkdir(parents=True)

    # Canonical per-station URL slug — THE single assignment both the /b/ page
    # builder (build_seo_stubs) and the client link generators (detail.js,
    # home.js) consume, so a name collision can never send a link to the wrong
    # station's page. Collision rule: first station (by station_id order, which
    # matches the stub builder's sorted glob) keeps the bare name-slug; later
    # ones get "-<sid[:6]>" (full sid if even that collides). Mirrors
    # scripts/seo_common.slug — slugs are user-facing URLs, frozen forever.
    from scripts.seo_common import slug as _name_slug
    _slug_seen: set[str] = set()

    def _assign_slug(sid: str, name) -> str:
        sl = _name_slug(name or sid)
        if sl in _slug_seen:
            sl = f"{sl}-{sid[:6]}"
        if sl in _slug_seen:                    # pathological: suffixed twin too
            sl = f"{_name_slug(name or sid)}-{sid}"
        _slug_seen.add(sl)
        return sl

    features, n_forecast, n_seasonal, n_no_data = [], 0, 0, 0
    for _, cat_row in cat.sort_values("station_id").iterrows():
        sid = str(cat_row["station_id"])
        series = _read_shard(inputs.shard_dir, sid)
        fcst_row = triage_by_sid.get(sid)
        if series.empty and fcst_row is None:
            n_no_data += 1                      # catalogued but never observed
            continue
        slug = _assign_slug(sid, cat_row.get("station_name"))

        st = status_from_series(series, sid, normals, now=now_ts.tz_localize(None))
        status = _status_block(st)

        fan_rows = None
        fcst = None
        if fcst_row is not None:
            fan_rows = (fan_all[fan_all["station_id"] == sid]
                        if fan_all is not None else None)
            fcst = _forecast_block(fcst_row, fan_rows)
            n_forecast += 1

        seas = None
        if seasonal_all is not None and not seasonal_all.empty:
            seas = _seasonal_block(
                seasonal_all[seasonal_all["station_id"] == sid])
            if seas is not None:
                n_seasonal += 1

        fresh = _freshness_block(fresh_by_sid.get(sid))
        tflag = _trend_flag_block(trend_by_sid.get(sid))
        station_norms = (normals[normals["station_id"] == sid]
                         if not normals.empty else None)
        month_norms = ({int(r["month"]): r for _, r in station_norms.iterrows()}
                       if station_norms is not None and not station_norms.empty else {})
        st_seq, op_seq = status_timeline(
            st["status"], fan_rows, seas["months"] if seas else None, month_norms)
        features.append(station_feature(cat_row, status, fresh, fcst,
                                        seas is not None, tflag, st_seq, op_seq,
                                        slug=slug))

        if include_history_for == "scope" and fcst is None:
            observed: list = []
        else:
            tail = series
            if not series.empty:
                cutoff = series.index.max() - pd.Timedelta(days=history_days)
                tail = series[series.index >= cutoff]
            observed = [[iso_date(d), jround(v, LEVEL_DP)]
                        for d, v in tail.items()]

        detail = station_detail(cat_row, status, fresh,
                                _normals_rows(station_norms),
                                observed, fcst, seas, st["month"], tflag,
                                slug=slug)
        _dump(detail, building / "stations" / f"{sid}.json", pretty=pretty)

    if not features:
        shutil.rmtree(building)
        raise ValueError("no stations with data — pack would be empty")

    _dump({"type": "FeatureCollection", "features": features},
          building / "stations.geojson", pretty=pretty)

    # Lightweight machine-readable catalogue (stations/index.json) — the geojson
    # identity/flag fields without the heavy per-frame payload, for API
    # consumers who just need "what stations exist and where". Additive (2026-07).
    _dump([{
        "station_id": f["properties"]["station_id"],
        "slug": f["properties"]["slug"],
        "name": f["properties"]["name"],
        "lat": f["geometry"]["coordinates"][1],
        "lon": f["geometry"]["coordinates"][0],
        "aquifer_designation": f["properties"]["aquifer_designation"],
        "has_forecast": f["properties"]["has_forecast"],
        "has_seasonal": f["properties"]["has_seasonal"],
    } for f in features], building / "stations" / "index.json", pretty=pretty)

    # Optional aquifer geology, copied verbatim (the explorer lazy-loads it).
    if inputs.geology_path is not None and inputs.geology_path.exists():
        shutil.copyfile(inputs.geology_path, building / "geology.geojson")

    forecast_run = None
    if summary is not None and not summary.empty:
        forecast_run = iso_utc(summary["run"].iloc[0])
    seasonal_run = None
    if seasonal_all is not None and not seasonal_all.empty:
        seasonal_run = {"run": iso_utc(seasonal_all["run"].iloc[0]),
                        "origin_date": iso_date(seasonal_all["origin_date"].iloc[0])}

    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_utc(now_ts),
        "region": inputs.region_name,
        "counts": {"stations": len(features), "with_forecast": n_forecast,
                   "with_seasonal": n_seasonal, "excluded": len(excluded),
                   "no_data": n_no_data},
        # Network-coverage audit (roadmap honorable mention) — honest disclosure
        # of how much of the monitored network is published / forecast / live, so
        # the explorer doesn't read as a cherry-picked demo. `live_capable` is the
        # network count of boreholes with an EA real-time feed (None if unknown);
        # `catalogued` = published + no-data + excluded.
        "coverage": {
            "catalogued": len(features) + n_no_data + len(excluded),
            "observed": len(features),
            "with_forecast": n_forecast,
            "no_data": n_no_data,
            "excluded": len(excluded),
            "live_capable": inputs.live_capable,
        },
        "runs": {"forecast": forecast_run, "seasonal": seasonal_run},
        "inputs": inputs.source_meta,
        "attribution": ATTRIBUTION,
        "disclaimer": DISCLAIMER,
        "history_days": history_days,
        "forecast_frames": [label for label, _ in TIMELINE_FRAMES],
        "forecast_frame_days": _frame_days(_seasonal_month_starts(seasonal_all),
                                           now=now_ts.tz_localize(None)),
    }
    _dump(meta, building / "meta.json", pretty=pretty)

    # National status history — one row per pack build day (below/near/above
    # counts over stations WITH a current status), appended to a small
    # append-only store and shipped in the pack for the landing sparkline /
    # any "X% below normal" headline. Additive contract file (2026-07).
    nat_row = {
        "date": iso_date(now_ts),
        "below": sum(1 for f in features if f["properties"]["status"] == "below"),
        "near": sum(1 for f in features if f["properties"]["status"] == "near"),
        "above": sum(1 for f in features if f["properties"]["status"] == "above"),
        "stations": len(features),
        "with_forecast": n_forecast,
    }
    hist_store = out_dir.parent / "national_history.json"
    nat_hist: list = []
    if hist_store.exists():
        try:
            nat_hist = json.loads(hist_store.read_text(encoding="utf-8"))
        except Exception:
            nat_hist = []
    nat_hist = [r for r in nat_hist if r.get("date") != nat_row["date"]] + [nat_row]
    nat_hist = nat_hist[-730:]                      # two years is plenty
    hist_store.write_text(json.dumps(nat_hist, separators=(",", ":")),
                          encoding="utf-8")
    _dump(nat_hist, building / "national_history.json", pretty=pretty)

    _dump(build_manifest(building), building / "manifest.json", pretty=pretty)

    # Two-rename swap. The old rmtree(out_dir)-then-rename left a multi-second
    # no-pack window (the live site 404s mid-publish) and, if killed between the
    # two calls, DESTROYED the previously-good pack with nothing to serve until
    # the next successful run. Renames are near-instant; the old tree survives
    # as .old until the new one is in place, and is restored on a failed swap.
    old = out_dir.parent / (out_dir.name + ".old")
    if old.exists():
        shutil.rmtree(old)                    # leftover from a crashed swap
    if out_dir.exists():
        out_dir.rename(old)
    try:
        building.rename(out_dir)
    except Exception:
        if old.exists() and not out_dir.exists():
            old.rename(out_dir)               # put yesterday's pack back
        raise
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    return meta
