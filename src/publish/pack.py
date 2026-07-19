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
    FLOW_FAN_KEY_MAP,
    FLOW_SUMMARY_COL_SOURCES,
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
from src.forecast.seasonal.normals import NORMALS_COLS, gw_monthly_normals

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
# RiverCast (Stage 7) — always present in meta once any flow input exists or
# not: a static caveat line, like ATTRIBUTION/DISCLAIMER above, so a consumer
# never has to null-check it. Required wording (build_plan.md honesty
# invariants): gauged flow / abstraction+discharge effects, rating-curve
# weakness at low flow, Q95-as-proxy (not a licence Hands-off-Flow value).
RIVER_DISCLAIMER = (
    "RiverCast flow data is gauged flow, including any abstraction and "
    "discharge effects, from Environment Agency hydrology stations (Open "
    "Government Licence v3.0). Rating curves — the stage-to-flow "
    "conversion — are least accurate at low flows. Q95 thresholds are "
    "climatological proxies (q95_proxy), not licence Hands-off-Flow values. "
    "Indicative and experimental; not for operational or safety-critical use."
)
# Mirrors src.download.flow's shard data_source marker (that module's
# constant is private; flow shards carry no live/dipped distinction to
# encode, so every row is this one value).
FLOW_DATA_SOURCE = "flow_logged"


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
    pastas_fan_archive: pd.DataFrame | None = None   # verification trail (parquet)
    seasonal: pd.DataFrame | None = None
    trend_flags: pd.DataFrame | None = None
    excluded_ids: frozenset[str] = frozenset()
    pinned_ids: frozenset[str] = frozenset()
    live_capable: int | None = None          # network count w/ an EA live feed
    region_name: str = ""
    source_meta: dict = field(default_factory=dict)
    geology_path: Path | None = None         # optional aquifer GeoJSON to ship
    rivers_path: Path | None = None          # optional river polylines (OS Open Rivers)
    # RiverCast (Stage 7) — all optional; absent/empty ⇒ zero flow stations,
    # the pack builds exactly as it does today (see docs/artifact_contract.md §6).
    flow_catalogue: pd.DataFrame | None = None
    flow_shard_dir: Path | None = None
    flow_summary: pd.DataFrame | None = None       # forecast_flow_summary.csv
    flow_fan: pd.DataFrame | None = None           # forecast_flow_fan.csv
    flow_gate: pd.DataFrame | None = None          # flow_fleet_scan.csv (rain_dependent)
    station_links: pd.DataFrame | None = None      # GW<->river inversion (RiverFlowMeasureID)


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
    # Verification trail: every archived fan (parquet, appended per daily run).
    # Optional — a fresh install has no archive yet; verification degrades to null.
    fan_archive = None
    fa_path = root / "data/model/forecast_pastas_fan_archive.parquet"
    if fa_path.exists():
        mtime = datetime.fromtimestamp(fa_path.stat().st_mtime, tz=timezone.utc)
        meta["pastas_fan_archive"] = {
            "path": fa_path.relative_to(root).as_posix(),
            "mtime_utc": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "ok"}
        try:
            fan_archive = pd.read_parquet(fa_path)
        except Exception:
            meta["pastas_fan_archive"]["status"] = "unreadable"
    else:
        meta["pastas_fan_archive"] = {
            "path": "data/model/forecast_pastas_fan_archive.parquet",
            "mtime_utc": None, "status": "missing"}
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

    # RiverCast (Stage 7) — every input here is OPTIONAL (graceful: a host
    # with no flow subsystem configured yet still builds a working GW-only
    # pack, see docs/artifact_contract.md §6). Provenance is tracked the same
    # way as every other optional input, under meta.inputs.<name>.
    flow_catalogue, meta["flow_catalogue"] = _source_entry(
        root / "data/processed/flow_catalogue.csv", root)
    flow_summary, meta["forecast_flow_summary"] = _source_entry(
        root / "data/model/forecast_flow_summary.csv", root)
    flow_fan, meta["forecast_flow_fan"] = _source_entry(
        root / "data/model/forecast_flow_fan.csv", root)
    flow_gate, meta["flow_fleet_scan"] = _source_entry(
        root / "outputs/flow_fleet_scan.csv", root)
    station_links, meta["station_links"] = _source_entry(
        root / "data/processed/station_links.csv", root)
    flow_shard_dir = root / "data/features/flow_by_station"
    if not flow_shard_dir.exists() or not any(flow_shard_dir.glob("*.parquet")):
        flow_shard_dir = None

    # Optional river-polyline context layer (rivers view in the explorer) —
    # OS Open Rivers (OGL), extracted + simplified per published gauge by
    # scripts/build_river_polylines.py. Same copied-verbatim, lazy-loaded,
    # never-required pattern as geology above.
    rivers = root / "data/processed/river_polylines.geojson"
    if rivers.exists():
        rmtime = datetime.fromtimestamp(rivers.stat().st_mtime, tz=timezone.utc)
        meta["river_polylines"] = {"path": rivers.relative_to(root).as_posix(),
                                   "mtime_utc": rmtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "status": "ok"}
    else:
        rivers = None
        meta["river_polylines"] = {"path": "data/processed/river_polylines.geojson",
                                   "mtime_utc": None, "status": "missing"}

    return PackInputs(
        catalogue=catalogue, shard_dir=shard_dir, freshness=freshness,
        normals=normals, pastas_summary=summary, pastas_fan=fan,
        pastas_fan_archive=fan_archive,
        seasonal=seasonal, trend_flags=trend_flags,
        excluded_ids=frozenset(excluded_station_ids()),
        pinned_ids=frozenset(user_threshold_station_ids()),
        live_capable=len(live_capable_ids()),
        region_name=cfg.get("region", {}).get("name", ""),
        source_meta=meta, geology_path=geology, rivers_path=rivers,
        flow_catalogue=flow_catalogue, flow_shard_dir=flow_shard_dir,
        flow_summary=flow_summary, flow_fan=flow_fan, flow_gate=flow_gate,
        station_links=station_links)


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


def _read_flow_shard(shard_dir: Path, gid: str) -> pd.Series:
    """Mirrors ``_read_shard`` for a flow gauge (``src.download.flow``'s
    ``Flow_m3s`` shard column). No stuck-telemetry filtering — flow shards
    carry no live-overlay concept the way GW does (module docstring of
    ``scripts/build_flow_members.py``)."""
    fp = shard_dir / f"{gid}.parquet"
    if not fp.exists():
        return pd.Series(dtype="float64", name="Flow_m3s")
    df = pd.read_parquet(fp, columns=["date", "Flow_m3s"])
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s = pd.Series(df["Flow_m3s"].to_numpy(float), index=idx.normalize(),
                  name="Flow_m3s").dropna()
    return s.sort_index()


# ---------------------------------------------------------------------------
# RiverCast (flow) helpers — Stage 7 of docs/product/lowflow/build_plan.md.
# Deliberately thin: status/normals reuse the GW routines UNCHANGED (generic
# over the value column — "reuse the GW normals approach" per the build plan),
# so a flow gauge is just another (series, station_id, normals) triple to them.
# ---------------------------------------------------------------------------

def _flow_monthly_normals(series: pd.Series, gid: str) -> pd.DataFrame:
    """Monthly flow climatology ladder for one gauge, via the exact GW
    routine (``gw_monthly_normals``) fed a same-shaped frame — no fork."""
    if series is None or series.empty:
        return pd.DataFrame(columns=NORMALS_COLS)
    joined = pd.DataFrame({"dateTime": series.index, "station_id": gid,
                           "GW_Level": series.to_numpy(float)})
    return gw_monthly_normals(joined)


def _flow_freshness_block(series: pd.Series | None, now: pd.Timestamp) -> dict:
    """Flow has no separate freshness-audit CSV (unlike GW's
    ``gw_freshness.csv``) — derive directly from the shard tail. Thresholds
    are a presentation convention (not a modelled quantity), chosen to read
    naturally against a daily-mean EA flow feed."""
    if series is None or series.empty:
        return {"label": None, "days_since": None,
                "last_real_reading": None, "data_source": None}
    last = pd.Timestamp(series.index.max())
    days = int((now.normalize() - last.normalize()).days)
    label = ("fresh" if days <= 2 else "recent" if days <= 7
             else "stale" if days <= 30 else "very_stale")
    return {"label": label, "days_since": jint(days),
            "last_real_reading": iso_date(last), "data_source": FLOW_DATA_SOURCE}


def _winterbourne_info(series: pd.Series | None,
                       zero_eps: float = 1e-4,
                       month_frac: float = 0.05) -> tuple[bool, list[int]]:
    """``(winterbourne, dry_months)`` from a gauge's own record: ``winterbourne``
    is true wherever the record has ANY zero-flow day (build_plan.md's literal
    trigger); ``dry_months`` are the calendar months where a zero/near-zero
    reading is common (>= ``month_frac`` of that month's observations across
    the whole record) — the "typically dry <months>" climatology read. Pure
    presentation of data already in the shard; no new model claim."""
    if series is None or series.empty:
        return False, []
    s = series.dropna()
    zero = s <= zero_eps
    if not bool(zero.any()):
        return False, []
    months = s.index.month
    dry = [m for m in range(1, 13)
          if (sel := zero[months == m]).size and float(sel.mean()) >= month_frac]
    return True, dry


def _linked_boreholes_map(links: pd.DataFrame | None) -> dict[str, list[str]]:
    """Invert ``station_links.csv``'s existing GW->river mapping
    (``RiverFlowMeasureID``) into gauge -> [borehole station_id, ...] — READ
    ONLY, station_links.csv itself is never modified. ``RiverFlowMeasureID``
    uses the EA *instantaneous* measure suffix (``-flow-i-900-m3s-qualified``)
    while the flow catalogue's own id uses the *daily-mean* suffix
    (``-flow-m-86400-m3s-qualified``); both share the same leading gauge GUID,
    so matching splits on the common ``-flow-`` separator rather than comparing
    the full measure id."""
    out: dict[str, list[str]] = {}
    if links is None or links.empty or "RiverFlowMeasureID" not in links.columns:
        return out
    for _, r in links.iterrows():
        rid = r.get("RiverFlowMeasureID")
        if rid is None or (isinstance(rid, float) and pd.isna(rid)):
            continue
        gauge_sid = str(rid).split("-flow-")[0]
        bh_sid = r.get("GWStationID")
        if bh_sid is None or (isinstance(bh_sid, float) and pd.isna(bh_sid)):
            continue
        out.setdefault(gauge_sid, []).append(str(bh_sid))
    return out


# ---------------------------------------------------------------------------
# Block builders (pure)
# ---------------------------------------------------------------------------

# National-history helpers, shared by the GW and flow accrual blocks below —
# the UKHO/NHMP band edges and the append-only store semantics must never
# diverge between the two files.

def _band_counts(pcts: list[float]) -> dict:
    """UKHO/NHMP 5-band counts (percentile cuts 13/28/72/87)."""
    return {
        "band_low": sum(1 for p in pcts if p < 13),
        "band_below": sum(1 for p in pcts if 13 <= p < 28),
        "band_normal": sum(1 for p in pcts if 28 <= p <= 72),
        "band_above": sum(1 for p in pcts if 72 < p <= 87),
        "band_high": sum(1 for p in pcts if p > 87),
    }


def _accrue_history(store_path: Path, row: dict, cap: int = 730) -> list:
    """Append ``row`` to the append-only national-history store at
    ``store_path`` (replace any same-date row; keep the newest ``cap``),
    write it back, and return the updated list for shipping in the pack.
    A corrupt store degrades to a fresh list rather than crashing a build."""
    hist: list = []
    if store_path.exists():
        try:
            hist = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist = [r for r in hist if r.get("date") != row["date"]] + [row]
    hist = hist[-cap:]
    store_path.write_text(json.dumps(hist, separators=(",", ":")),
                          encoding="utf-8")
    return hist


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


# Verification needs this many OBSERVED days inside a closed window before we
# score it — a couple of stray readings can't honestly grade a 14-day fan.
MIN_VERIFY_OBS = 8


def _verification_block(vdf: pd.DataFrame | None, series: pd.Series | None,
                        now_naive: pd.Timestamp) -> dict | None:
    """'How did the last forecast do?' — the most recent ARCHIVED fan whose
    whole window has closed and which has >= MIN_VERIFY_OBS observed days,
    scored against those observations. None when the archive/observations
    can't support an honest comparison (young archive, stale sensor, no
    forecast). The credibility block: published as-is, good or bad.

    ``vdf``: this station's archived FORECAST-segment fan rows (all runs).
    ``series``: the station's observed daily series (tz-naive date index).
    """
    if vdf is None or vdf.empty or series is None or series.empty:
        return None
    # Most recent run first; stop at the first scoreable closed window.
    for run in sorted(vdf["run"].unique())[::-1]:
        g = vdf[vdf["run"] == run].sort_values("lead")
        dates = pd.DatetimeIndex(pd.to_datetime(g["date"]))
        if dates.tz is not None:
            dates = dates.tz_localize(None)
        if dates.max() >= now_naive.normalize():
            continue                                   # window not closed yet
        obs = series.reindex(dates)
        valid = obs.notna().to_numpy()
        n_obs = int(valid.sum())
        if n_obs < MIN_VERIFY_OBS:
            continue                                   # too few obs to grade
        o = obs.to_numpy(float)[valid]
        p10 = g["gw_p10"].to_numpy(float)[valid]
        p50 = g["gw_p50"].to_numpy(float)[valid]
        p90 = g["gw_p90"].to_numpy(float)[valid]
        n_in = int(((o >= p10) & (o <= p90)).sum())
        mae = float(abs(o - p50).mean())
        return {
            "run": iso_utc(run),
            "origin_date": iso_date(dates.min() - pd.Timedelta(days=1)),
            "horizon_days": int(len(g)),
            "n_obs": n_obs,
            "n_in_band": n_in,
            "mae_p50": jround(mae, 3),
            "fan": [{"lead": jint(r["lead"]), "date": iso_date(r["date"]),
                     "p10": jround(r["gw_p10"], LEVEL_DP),
                     "p50": jround(r["gw_p50"], LEVEL_DP),
                     "p90": jround(r["gw_p90"], LEVEL_DP)}
                    for _, r in g.iterrows()],
        }
    return None


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
    # Short-record fan tier (< MIN_ROWS obs, admitted by the hindcast gate):
    # the fan is real but provisional (wider bands, no seasonal) — the borehole
    # page badges it so the shorter record is never mistaken for a mature one.
    out["short_record"] = jbool(srow.get("short_record", False))

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


def _flow_forecast_block(srow: pd.Series, fan: pd.DataFrame | None,
                         rain_dependent: bool) -> dict:
    """Flow analogue of ``_forecast_block`` — same lift-and-round pattern,
    driven by FLOW_SUMMARY_COL_SOURCES/FLOW_FAN_KEY_MAP instead of the GW
    constants, plus ``rain_dependent`` (the Stage-4 gate's tier flag, not a
    summary column, so it isn't in the source-column mapping)."""
    out: dict = {}
    for key, col in FLOW_SUMMARY_COL_SOURCES.items():
        val = srow.get(col)
        if key == "run":
            out[key] = iso_utc(val)
        elif key in ("origin_date", "first_cross_median",
                     "first_cross_p25", "first_cross_p75"):
            out[key] = iso_date(val)
        elif key in ("threshold", "q_p50_end"):
            out[key] = jround(val, LEVEL_DP)
        elif key in ("p_below_q95", "p_below_q95_14d", "censored_frac"):
            out[key] = jround(val, PROB_DP)
        elif key in ("stale_days", "horizon_days", "first_cross_median_lead",
                     "n_members", "n_samples"):
            out[key] = jint(val)
        else:                                   # threshold_source, headline
            out[key] = _str_or_none(val)
    out["rain_dependent"] = jbool(rain_dependent)

    rows = []
    if fan is not None and not fan.empty:
        for _, r in fan.sort_values("lead").iterrows():
            entry = {"lead": jint(r["lead"]), "date": iso_date(r["date"]),
                     "segment": _str_or_none(r.get("segment")) or "forecast"}
            for src, dst in FLOW_FAN_KEY_MAP.items():
                entry[dst] = jround(r.get(src), LEVEL_DP)
            rows.append(entry)
    out["fan"] = rows
    return out


def flow_station_feature(cat_row: pd.Series, status: dict, fresh: dict,
                         rain_dependent: bool, slug: str,
                         seasonal_winterbourne: bool = False) -> dict:
    """One GeoJSON Feature for a RiverCast gauge — a deliberately SMALLER,
    distinct property set from a GW feature (docs/artifact_contract.md §5.3):
    no aquifer/tier/threshold/timeline props (GW-specific vocabulary), and
    ``station_type: "flow"`` is the only key a GW feature never carries.
    ``seasonal_winterbourne`` (published as ``winterbourne``, additive
    2026-07-19) is the SEASONAL read — dry_months non-empty — NOT the
    detail's literal any-zero-day ``station.winterbourne`` flag; the
    parameter name is deliberately different so the two booleans can't be
    swapped silently at a call site."""
    props = {
        "station_id": str(cat_row["station_id"]),
        "slug": slug,
        "name": _str_or_none(cat_row.get("station_name")),
        "station_type": "flow",
        **status,
        "freshness": fresh["label"],
        "days_since": fresh["days_since"],
        "data_source": fresh["data_source"],
        "has_forecast": True,          # v1: only gated, fan-carrying gauges publish
        "river_name": _str_or_none(cat_row.get("river_name")),
        "rain_dependent": jbool(rain_dependent),
        "winterbourne": jbool(seasonal_winterbourne),
    }
    return {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [jround(cat_row["lon"], 6),
                                     jround(cat_row["lat"], 6)]},
        "properties": props,
    }


def flow_station_detail(cat_row: pd.Series, status: dict, fresh: dict,
                        normals_rows: list[dict], observed: list,
                        fcst: dict, status_month: int | None, slug: str,
                        linked_boreholes: list[str], winterbourne: bool,
                        dry_months: list[int]) -> dict:
    """Same top-level envelope as ``station_detail`` (docs/artifact_contract.md
    §5.4a): seasonal/trend_flag/verification are always null in v1 (no public
    flow seasonal — Stage 6b is shadow-only; no trend screen; no closed
    verification window yet)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "station": {
            "station_id": str(cat_row["station_id"]),
            "slug": slug,
            "name": _str_or_none(cat_row.get("station_name")),
            "lat": jround(cat_row["lat"], 6),
            "lon": jround(cat_row["lon"], 6),
            "station_type": "flow",
            "river_name": _str_or_none(cat_row.get("river_name")),
            "linked_boreholes": linked_boreholes,
            "winterbourne": jbool(winterbourne),
            "dry_months": [jint(m) for m in dry_months],
        },
        "status": {**status, "month": status_month},
        "freshness": fresh,
        "normals": normals_rows,
        "observed": {"unit": "m3/s", "series": observed},
        "forecast": fcst,
        "seasonal": None,
        "trend_flag": None,
        "verification": None,
    }


# A seasonal outlook whose anchor is this much older than the pack build is
# stale — refuse to publish it (the 2026-07-09 bug: outlooks seeded at
# months-old observations, served under a current run stamp). Anchors ~14 days
# in the FUTURE are normal: the ESP is anchored at the 14-day fan terminal.
SEASONAL_MAX_ANCHOR_AGE_D = 60


def _seasonal_block(srows: pd.DataFrame,
                    now: pd.Timestamp | None = None) -> dict | None:
    if srows is None or srows.empty:
        return None
    srows = srows.sort_values("month_ahead")
    first = srows.iloc[0]
    if now is not None:
        # stale anchor → no outlook (an honest null beats a months-old tercile)
        try:
            age_d = (now.normalize() - pd.Timestamp(first["origin_date"])).days
        except (TypeError, ValueError):
            age_d = None
        if age_d is not None and age_d > SEASONAL_MAX_ANCHOR_AGE_D:
            return None
        # drop months already over (the month in progress stays) — an outlook
        # for the past is not an outlook
        month0 = now.normalize().replace(day=1)
        srows = srows[pd.to_datetime(srows["month_start"]) >= month0]
        if srows.empty:
            return None
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


def _dominant_origin(df: pd.DataFrame):
    """The modal origin_date — the run's fleet-uniform anchor. Under mixed
    origins (an archive carrying stale per-borehole seeds) any single-row
    pick is arbitrary; the mode is the cohort the run actually produced.
    None when the column is absent or all-NaN."""
    if "origin_date" not in df.columns or not df["origin_date"].notna().any():
        return None
    mode = df["origin_date"].mode()
    return mode.iloc[0] if len(mode) else None


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
    dominant = _dominant_origin(df)
    if dominant is not None:
        df = df[df["origin_date"] == dominant]
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
    """Category (of P50) + confidence (P10/P50/P90 agreement) at a fan lead.

    Distance ties break toward the LOWER lead: the fan has no lead-0 row
    (forecast leads 1.., nowcast leads ..-1), so the frame-0 "Today" lookup
    must resolve to the nowcast -1 row, not the forecast +1 (tomorrow) row
    that happens to be appended first."""
    leads = fan["lead"].to_numpy()
    pos = int(np.lexsort((leads, np.abs(leads - lead)))[0])
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
        "short_record": fcst["short_record"] if fcst else False,
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
                   slug: str | None = None,
                   verification: dict | None = None) -> dict:
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
        "verification": verification,
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

    # Verification trail: slice the archived FORECAST fans once, grouped by
    # station — _verification_block picks each station's newest closed window.
    verify_by_sid: dict[str, pd.DataFrame] = {}
    if (inputs.pastas_fan_archive is not None
            and not inputs.pastas_fan_archive.empty):
        va = inputs.pastas_fan_archive
        if "segment" in va.columns:
            va = va[va["segment"] == "forecast"]
        verify_by_sid = {str(k): g for k, g in va.groupby("station_id")}

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
                seasonal_all[seasonal_all["station_id"] == sid],
                now=now_ts.tz_localize(None))
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

        verification = _verification_block(verify_by_sid.get(sid), series,
                                           now_ts.tz_localize(None))
        detail = station_detail(cat_row, status, fresh,
                                _normals_rows(station_norms),
                                observed, fcst, seas, st["month"], tflag,
                                slug=slug, verification=verification)
        _dump(detail, building / "stations" / f"{sid}.json", pretty=pretty)

    # -----------------------------------------------------------------------
    # RiverCast (flow gauges) — Stage 7. GRACEFUL: any missing input among
    # flow_catalogue / flow_summary / flow_fan means zero flow stations, the
    # pack builds exactly as it does without this block (docs §6). v1 launch
    # scope: a flow station is published ONLY when it has a fan (its own
    # gate-driven admission — no "outside forecast scope" tier for rivers).
    #
    # n_gw_features is snapshotted BEFORE flow features are appended:
    # meta.counts.stations / meta.coverage stay GW-ONLY (their long-documented
    # semantics — changing what they count would be a semantic change to an
    # existing key, which the contract's change policy requires a version
    # bump for). Flow gets its OWN counters (flow_gauges/flow_with_forecast).
    # -----------------------------------------------------------------------
    n_gw_features = len(features)
    n_flow, n_flow_forecast = 0, 0
    # Per-gauge snapshot rows for flow_national_history.json (accrued below,
    # after the GW national-history block): status/percentile vs the gauge's
    # OWN flow climatology + a below-its-own-Q95-right-now flag. Collected
    # here rather than re-derived from `features` because Q95 (the summary's
    # q95_m3s) is not a feature property.
    flow_hist_rows: list[dict] = []
    flow_summary_latest = _latest_run_only(inputs.flow_summary)
    if (flow_summary_latest is not None and not flow_summary_latest.empty
            and inputs.flow_catalogue is not None
            and not inputs.flow_catalogue.empty):
        fcat = inputs.flow_catalogue.drop_duplicates("station_id")
        fcat = fcat[fcat["lat"].notna() & fcat["lon"].notna()]
        fcat_by_gid = {str(r["station_id"]): r for _, r in fcat.iterrows()}
        flow_fan_all = (inputs.flow_fan if inputs.flow_fan is not None
                        else pd.DataFrame(columns=["gauge_id"]))
        rain_dep_by_gid: dict[str, bool] = {}
        if (inputs.flow_gate is not None and not inputs.flow_gate.empty
                and "rain_dependent" in inputs.flow_gate.columns):
            rain_dep_by_gid = {str(r["gauge_id"]): bool(r["rain_dependent"])
                              for _, r in inputs.flow_gate.iterrows()}
        linked_by_gid = _linked_boreholes_map(inputs.station_links)

        for gid in sorted(flow_summary_latest["gauge_id"].astype(str).unique()):
            cat_row = fcat_by_gid.get(gid)
            if cat_row is None:
                continue                       # summary row with no catalogue entry
            fan_rows = (flow_fan_all[flow_fan_all["gauge_id"] == gid]
                       if not flow_fan_all.empty else None)
            if fan_rows is None or fan_rows.empty:
                continue                       # no fan -> gauge doesn't publish (launch scope)
            srow = (flow_summary_latest[flow_summary_latest["gauge_id"] == gid]
                    .iloc[0])
            series = (_read_flow_shard(inputs.flow_shard_dir, gid)
                     if inputs.flow_shard_dir is not None else pd.Series(dtype="float64"))
            slug = _assign_slug(gid, cat_row.get("station_name"))

            gauge_norms = _flow_monthly_normals(series, gid)
            st = status_from_series(series, gid, gauge_norms,
                                    now=now_ts.tz_localize(None))
            status = _status_block(st)
            fresh = _flow_freshness_block(series, now_ts.tz_localize(None))
            rain_dep = rain_dep_by_gid.get(gid, False)
            fcst = _flow_forecast_block(srow, fan_rows, rain_dep)
            winterbourne, dry_months = _winterbourne_info(series)
            n_flow += 1
            n_flow_forecast += 1               # every published flow station carries a fan (v1)

            q95 = srow.get("q95_m3s")
            level_now = st.get("level")
            flow_hist_rows.append({
                "status": st.get("status"),
                "percentile": st.get("percentile"),
                # "below Q95 RIGHT NOW" requires a CURRENT reading — the same
                # population rule as below/near/above (status is None when the
                # latest observation is too old to place). Without the status
                # gate, a gauge whose feed died during a dry spell would count
                # as "below Q95 now" forever off its months-old last reading.
                "below_q95_now": (st.get("status") is not None
                                  and level_now is not None and q95 is not None
                                  and pd.notna(level_now) and pd.notna(q95)
                                  and float(level_now) < float(q95)),
            })

            # The FEATURE flag is the stricter seasonal read (a recurring dry
            # season, i.e. dry_months non-empty), not the detail's literal
            # any-zero-day trigger — a single zero-flow reading (datum or
            # diversion artifact) must not put a river in the winterbourne
            # story on the landing/map.
            features.append(flow_station_feature(cat_row, status, fresh,
                                                  rain_dep, slug,
                                                  bool(dry_months)))

            tail = series
            if not series.empty:
                cutoff = series.index.max() - pd.Timedelta(days=history_days)
                tail = series[series.index >= cutoff]
            observed = [[iso_date(d), jround(v, LEVEL_DP)]
                        for d, v in tail.items()]

            detail = flow_station_detail(
                cat_row, status, fresh, _normals_rows(gauge_norms), observed,
                fcst, st["month"], slug, linked_by_gid.get(gid, []),
                winterbourne, dry_months)
            _dump(detail, building / "stations" / f"{gid}.json", pretty=pretty)

    if not features:
        shutil.rmtree(building)
        raise ValueError("no stations with data — pack would be empty")

    _dump({"type": "FeatureCollection", "features": features},
          building / "stations.geojson", pretty=pretty)

    # Lightweight machine-readable catalogue (stations/index.json) — the geojson
    # identity/flag fields without the heavy per-frame payload, for API
    # consumers who just need "what stations exist and where". Additive (2026-07).
    # A flow row additionally carries "station_type": "flow" (absent for GW,
    # same "absent means gw" rule as the geojson — see docs §2.1/§5.3); flow
    # features have no aquifer/seasonal concept, hence the .get() defaults.
    def _index_row(f: dict) -> dict:
        p = f["properties"]
        row = {
            "station_id": p["station_id"], "slug": p["slug"], "name": p["name"],
            "lat": f["geometry"]["coordinates"][1],
            "lon": f["geometry"]["coordinates"][0],
            "aquifer_designation": p.get("aquifer_designation"),
            "has_forecast": p["has_forecast"],
            "has_seasonal": p.get("has_seasonal", False),
        }
        if p.get("station_type"):
            row["station_type"] = p["station_type"]
        return row
    _dump([_index_row(f) for f in features],
          building / "stations" / "index.json", pretty=pretty)

    # Optional aquifer geology, copied verbatim (the explorer lazy-loads it).
    if inputs.geology_path is not None and inputs.geology_path.exists():
        shutil.copyfile(inputs.geology_path, building / "geology.geojson")

    # Optional river polylines (rivers view), copied verbatim — same lazy
    # pattern as geology.
    if inputs.rivers_path is not None and inputs.rivers_path.exists():
        shutil.copyfile(inputs.rivers_path, building / "rivers.geojson")

    forecast_run = None
    if summary is not None and not summary.empty:
        forecast_run = iso_utc(summary["run"].iloc[0])
    seasonal_run = None
    if seasonal_all is not None and not seasonal_all.empty:
        # origin_date = the dominant cohort's anchor, not iloc[0]: a mixed
        # archive (stale per-borehole seeds) sorts arbitrarily, and the first
        # row can stamp a months-old origin on a current run.
        seasonal_run = {"run": iso_utc(seasonal_all["run"].iloc[0]),
                        "origin_date": iso_date(_dominant_origin(seasonal_all))}

    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_utc(now_ts),
        "region": inputs.region_name,
        "counts": {"stations": n_gw_features, "with_forecast": n_forecast,
                   "with_seasonal": n_seasonal, "excluded": len(excluded),
                   "no_data": n_no_data, "flow_gauges": n_flow,
                   "flow_with_forecast": n_flow_forecast},
        # Network-coverage audit (roadmap honorable mention) — honest disclosure
        # of how much of the monitored network is published / forecast / live, so
        # the explorer doesn't read as a cherry-picked demo. `live_capable` is the
        # network count of boreholes with an EA real-time feed (None if unknown);
        # `catalogued` = published + no-data + excluded. GW-only (see the
        # RiverCast block above) — unchanged semantics, pre-dates flow gauges.
        "coverage": {
            "catalogued": n_gw_features + n_no_data + len(excluded),
            "observed": n_gw_features,
            "with_forecast": n_forecast,
            "no_data": n_no_data,
            "excluded": len(excluded),
            "live_capable": inputs.live_capable,
        },
        "runs": {"forecast": forecast_run, "seasonal": seasonal_run},
        "inputs": inputs.source_meta,
        "attribution": ATTRIBUTION,
        "disclaimer": DISCLAIMER,
        "river_disclaimer": RIVER_DISCLAIMER,
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
    # GW-only (matches meta.counts.stations above) — a flow feature's own
    # "status" is against a completely different climatology (flow, not
    # head), so folding it into the same below/near/above tally would quietly
    # change what this file has always meant, without a version bump.
    gw_features = [f for f in features if not f["properties"].get("station_type")]
    nat_row = {
        "date": iso_date(now_ts),
        "below": sum(1 for f in gw_features if f["properties"]["status"] == "below"),
        "near": sum(1 for f in gw_features if f["properties"]["status"] == "near"),
        "above": sum(1 for f in gw_features if f["properties"]["status"] == "above"),
        "stations": n_gw_features,
        "with_forecast": n_forecast,
    }
    # UKHO/NHMP 5-band counts (percentile cuts 13/28/72/87) — additive keys
    # accruing since 2026-07-18 so a future 5-band display has history from
    # day one (this store cannot be backfilled: the per-borehole percentiles
    # are overwritten each build). Same population as below/near/above — GW
    # features carrying a current status AND a percentile. Consumers ignore
    # unknown keys (renderTrend reads below/near/above only).
    pcts = [float(f["properties"]["percentile"]) for f in gw_features
            if f["properties"].get("status")
            and f["properties"].get("percentile") is not None]
    nat_row.update(_band_counts(pcts))
    nat_hist = _accrue_history(out_dir.parent / "national_history.json", nat_row)
    _dump(nat_hist, building / "national_history.json", pretty=pretty)

    # Flow national status history (flow_national_history.json) — the
    # RiverCast analogue of the block above, in its OWN file because the GW
    # file's below/near/above population is long-documented as GW-only (see
    # that block's comment). One row per pack-build day over the published
    # flow gauges: below/near/above vs each gauge's OWN flow climatology,
    # the UKHO-style 5-band counts (the 2026-07-18 lesson: accrue bands from
    # day one — this store cannot be backfilled), and `n_below_q95_now`
    # (gauges whose latest observation is under their own Q95 proxy).
    # Additive contract file (2026-07-19). Skipped entirely when the pack has
    # no flow stations — a GW-only host accrues no misleading all-zero rows,
    # and its pack contents stay byte-identical to before RiverCast.
    if flow_hist_rows:
        flow_nat_row = {
            "date": iso_date(now_ts),
            "below": sum(1 for r in flow_hist_rows if r["status"] == "below"),
            "near": sum(1 for r in flow_hist_rows if r["status"] == "near"),
            "above": sum(1 for r in flow_hist_rows if r["status"] == "above"),
            "n_gauges": n_flow,
            "n_with_forecast": n_flow_forecast,
            "n_below_q95_now": sum(1 for r in flow_hist_rows if r["below_q95_now"]),
        }
        fpcts = [float(r["percentile"]) for r in flow_hist_rows
                 if r["status"] and r["percentile"] is not None
                 and pd.notna(r["percentile"])]
        flow_nat_row.update(_band_counts(fpcts))
        flow_nat_hist = _accrue_history(
            out_dir.parent / "flow_national_history.json", flow_nat_row)
        _dump(flow_nat_hist, building / "flow_national_history.json", pretty=pretty)

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
