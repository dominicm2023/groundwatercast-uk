"""Flow-gauge ingest + shard build/top-up — Stage 2 of the low-flow Rivers
layer build (``docs/product/lowflow/build_plan.md``).

Flow gauges are first-class stations with their own raw archive and their own
Parquet shards (architecture decision 1). This module reuses
``src.download.build``'s HTTP primitives (``download_measure``,
``topup_measure``, ``extract_measure_ids``) completely unchanged — a flow
measure id is just another ``measure_type`` ("flow") under
``data/raw/flow/<measure_id>.csv``, using the ``"flow": ["FlowMeasureID"]``
entry added to ``_TYPE_COLUMNS``.

The shard writer differs from the GW pipeline's, by necessity: GW shards are
built from ``joined_timeseries.csv`` (the output of the full features build,
which merges logged + dipped readings) and then topped up surgically
(``scripts/refresh_gw_shard_tail.py``). Flow has no equivalent features
pipeline — one raw measure CSV per gauge is the whole series — so
``build_or_topup_flow_shard`` does both jobs in one idempotent step: build
the shard from scratch the first time a gauge is seen, append its raw tail
on every later call.

Values are m3/s, daily mean. Zero and near-zero flows are KEPT — winterbournes
legitimately dry out. No screening, no log transform: logs happen at Pastas
fit time (Stage 3), with the epsilon handling documented there.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.data.pet import fetch_station_pet
from src.download.build import download_measure, topup_measure
from src.forecast.ensemble.members import gauge_rainfall_for

FLOW_LINKS_PATH = Path("data/processed/flow_links.csv")
FLOW_SHARD_DIR = Path("data/features/flow_by_station")
FLOW_CATALOGUE_PATH = Path("data/processed/flow_catalogue.csv")
_PET_LAG_DAYS = 5   # mirrors src.data.pet's archive lag

# On-disk default for the low-flow pilot CSV (scripts/select_flow_pilot.py's
# OUT_PATH) — the fallback every ``resolve_flow_pilot_path`` caller uses when
# config doesn't override it.
DEFAULT_FLOW_PILOT_PATH = "data/processed/flow_pilot.csv"


def resolve_flow_pilot_path(cfg: dict, root: Path = Path(".")) -> Path:
    """Single source of truth for the low-flow pilot CSV location, read from
    ``forecast.ensemble.flow.pilot_path`` (falling back to
    ``DEFAULT_FLOW_PILOT_PATH``) — every flow-pilot consumer must resolve
    this identically or they silently point at different pilot sets.
    ``scripts/build_flow_seasonal_shadow.py`` and
    ``scripts/refresh_seasonal_inputs.py`` originated this exact lookup;
    ``scripts/build_ensemble_members.py``'s ``build_flow_ens_bridge`` call
    site and ``scripts/build_flow_models.py``'s ``--pilot`` default now use
    this helper too, so all four resolve the path the same way.

    ``root`` prefixes a relative config value with each script's own repo
    root; an already-absolute config value passes through unchanged
    (``Path.__truediv__``'s standard behaviour).
    """
    fcfg = cfg.get("forecast", {}).get("ensemble", {}).get("flow", {})
    return Path(root) / fcfg.get("pilot_path", DEFAULT_FLOW_PILOT_PATH)


# Same shape family as the GW shards (date, value, provenance) — no
# is_interpolated column: flow has no dipped/logged distinction to encode,
# every row here is the same EA daily-mean *-qualified reading.
SHARD_COLS = ["date", "Flow_m3s", "data_source"]
_DATA_SOURCE = "flow_logged"


# ---------------------------------------------------------------------------
# gauge_id -> FlowMeasureID
# ---------------------------------------------------------------------------

def load_flow_measure_map(links_path: Path = FLOW_LINKS_PATH) -> dict[str, str]:
    """``{GaugeID: FlowMeasureID}`` from ``flow_links.csv``, NaN rows dropped.

    Ids are treated as opaque strings throughout — never parsed/truncated
    (some are compound ``<guid>_<suffix>`` ids for split-channel gauges).
    """
    df = pd.read_csv(links_path, usecols=["GaugeID", "FlowMeasureID"], dtype=str).dropna()
    return dict(zip(df["GaugeID"], df["FlowMeasureID"]))


# ---------------------------------------------------------------------------
# Raw archive top-up (reuses src.download.build unchanged)
# ---------------------------------------------------------------------------

def ensure_flow_raw_current(measure_id: str, config: dict) -> tuple[str, str]:
    """Download the raw flow CSV if it doesn't exist yet, else top up its
    tail to today. Returns ``(measure_id, status)`` — one of
    'downloaded'/'chunked'/'failed' (first download) or
    'advanced'/'current'/'absent'/'failed' (top-up). A 0-byte/unparseable
    existing raw file is healed transparently by topup_measure, which falls
    through to a full download in that case — so this can also surface
    'downloaded'/'chunked' via the top-up branch, not just the first-download
    one."""
    raw_root = Path(config["download"]["raw_root"])
    out_path = raw_root / "flow" / f"{measure_id}.csv"
    if out_path.exists():
        return topup_measure(measure_id, "flow", config)
    return download_measure(measure_id, "flow", config)


# ---------------------------------------------------------------------------
# Shard writer (build-from-scratch + top-up, one function)
# ---------------------------------------------------------------------------

def _daily_mean_from_raw(raw_path: Path, after: pd.Timestamp | None) -> pd.DataFrame:
    """Daily-mean m3/s rows from a raw flow readings CSV, for dates strictly
    after ``after`` (all dates when ``after`` is None). Mirrors the GW
    daily-aggregation shape (UTC -> naive date, group-mean) but keeps every
    finite value, including exact zero — winterbournes dry out for real.

    A 0-byte raw file (the EA API occasionally returns ``200`` with an empty
    body for a catalogued-but-not-yet-populated qualified measure — seen live
    on the 2026-07-14 50-gauge pilot ingest) is treated as "no data yet", not
    an error: returns the empty frame instead of raising.
    """
    try:
        df = pd.read_csv(raw_path, usecols=["dateTime", "value"], low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=SHARD_COLS)
    dt = pd.to_datetime(df["dateTime"], utc=True, errors="coerce")
    frame = pd.DataFrame({
        "date": dt.dt.tz_localize(None).dt.normalize(),
        "value": pd.to_numeric(df["value"], errors="coerce"),
    }).dropna(subset=["date", "value"])
    if after is not None:
        frame = frame[frame["date"] > after]
    if frame.empty:
        return pd.DataFrame(columns=SHARD_COLS)
    daily = (frame.groupby("date", as_index=False)["value"].mean()
             .rename(columns={"value": "Flow_m3s"}))
    daily["data_source"] = _DATA_SOURCE
    return daily[SHARD_COLS]


def build_or_topup_flow_shard(gauge_id: str, raw_path: Path,
                              shard_dir: Path = FLOW_SHARD_DIR) -> tuple[str, int]:
    """Build the gauge's shard from scratch if absent, else append its raw
    tail beyond the shard's last date. Returns ``(status, n_rows_added)``:
    'built' (fresh shard written), 'advanced' (tail appended), 'current'
    (nothing new), or 'no_raw' (raw CSV missing/empty)."""
    if not raw_path.exists():
        return "no_raw", 0

    shard_dir = Path(shard_dir)
    fp = shard_dir / f"{gauge_id}.parquet"

    if not fp.exists():
        daily = _daily_mean_from_raw(raw_path, after=None)
        if daily.empty:
            return "no_raw", 0
        daily = daily.sort_values("date").reset_index(drop=True)
        shard_dir.mkdir(parents=True, exist_ok=True)
        daily.to_parquet(fp, compression="snappy", index=False)
        return "built", len(daily)

    existing = pd.read_parquet(fp)
    last = existing["date"].max() if not existing.empty else None
    new = _daily_mean_from_raw(raw_path, after=last)
    if new.empty:
        return "current", 0

    merged = (pd.concat([existing, new], ignore_index=True)
              .drop_duplicates(subset=["date"], keep="last")
              .sort_values("date").reset_index(drop=True))
    tmp = fp.with_suffix(fp.suffix + ".tmp")   # atomic swap, mirrors refresh_gw_shard_tail
    merged.to_parquet(tmp, compression="snappy", index=False)
    os.replace(tmp, fp)
    return "advanced", len(new)


# ---------------------------------------------------------------------------
# Per-gauge orchestrator (never raises — caller aggregates a tally)
# ---------------------------------------------------------------------------

def refresh_flow_gauge(gauge_id: str, measure_id: str, config: dict,
                       shard_dir: Path = FLOW_SHARD_DIR) -> dict:
    """Ensure the gauge's raw archive is current, then build/top-up its
    shard. Returns a per-gauge summary dict; never raises."""
    try:
        _, dl_status = ensure_flow_raw_current(measure_id, config)
    except Exception as exc:
        return {"gauge_id": gauge_id, "download": "failed", "shard": "error",
                "n_rows": 0, "error": str(exc)}

    raw_root = Path(config["download"]["raw_root"])
    raw_path = raw_root / "flow" / f"{measure_id}.csv"
    try:
        shard_status, n_rows = build_or_topup_flow_shard(gauge_id, raw_path, shard_dir)
    except Exception as exc:
        return {"gauge_id": gauge_id, "download": dl_status, "shard": "error",
                "n_rows": 0, "error": str(exc)}

    return {"gauge_id": gauge_id, "download": dl_status, "shard": shard_status,
            "n_rows": n_rows}


# ---------------------------------------------------------------------------
# Per-gauge model-input assembly (q/prec/evap) — shared by
# scripts/flow_gate_check.py (Stage 4) and scripts/flow_fleet_scan.py
# (Stage 5). Ingests whatever's missing (flow shard via refresh_flow_gauge
# above, rain raw archives via src.download.build unchanged, PET via
# src.data.pet unchanged) and hands back plain pandas Series ready for
# src.forecast.pastas.flow_gate.admit_gauge. Never raises: callers get None
# for "can't be assembled", not an exception.
# ---------------------------------------------------------------------------

def ensure_rain_raw(links_row: pd.Series, config: dict, *,
                    download_lock=None) -> None:
    """Download any of the gauge's 3 nearest-rain-gauge raw archives that
    aren't cached yet (Stage-2/Stage-1 machinery, reused unchanged).

    ``download_lock`` (any context-manager-like lock, e.g.
    ``multiprocessing.Manager().Lock()``) is optional and only needed by
    parallel callers: nearby gauges commonly share a nearest rain gauge, so
    two worker processes can both see the raw CSV missing and both start a
    download of the SAME file — ``download_measure``/``_stream_to_file``
    aren't atomic, so a concurrent write can corrupt it. Serial callers
    (e.g. flow_gate_check.py) pass ``None`` and pay no locking cost.
    """
    for col in ("RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3"):
        mid = links_row.get(col)
        if mid is None or (isinstance(mid, float) and pd.isna(mid)):
            continue
        raw_path = Path(config["download"]["raw_root"]) / "rainfall" / f"{mid}.csv"
        if raw_path.exists():
            continue
        if download_lock is None:
            download_measure(str(mid), "rainfall", config)
            continue
        with download_lock:
            if raw_path.exists():        # another worker may have just finished it
                continue
            download_measure(str(mid), "rainfall", config)


def load_gauge_series(gauge_id: str, links_df: pd.DataFrame, cat_df: pd.DataFrame,
                      measure_map: dict[str, str], config: dict, *,
                      download_lock=None,
                      ) -> tuple[pd.Series, pd.Series, pd.Series] | None:
    """Assemble (flow, rain, pet) for one gauge, ingesting whatever's missing.
    Returns None if the gauge can't be assembled (missing catalogue row,
    measure id, or any series ends up empty). Never raises internally beyond
    what its callees raise on genuinely unexpected errors — callers that run
    this unattended (flow_fleet_scan.py) wrap the call themselves."""
    if gauge_id not in cat_df.index or gauge_id not in measure_map:
        return None

    fp = FLOW_SHARD_DIR / f"{gauge_id}.parquet"
    if not fp.exists():
        refresh_flow_gauge(gauge_id, measure_map[gauge_id], config)
    if not fp.exists():
        return None
    shard = pd.read_parquet(fp)
    if shard.empty:
        return None
    q = pd.Series(shard["Flow_m3s"].to_numpy(float),
                 index=pd.to_datetime(shard["date"]), name="Flow_m3s")

    if gauge_id not in links_df.index:
        return None
    ensure_rain_raw(links_df.loc[gauge_id], config, download_lock=download_lock)
    prec = gauge_rainfall_for(gauge_id, links_df, config["download"]["raw_root"])
    if prec.empty:
        return None

    lat = float(cat_df.loc[gauge_id, "lat"])
    lon = float(cat_df.loc[gauge_id, "lon"])
    start = pd.Timestamp(q.index.min()).date()
    end = date.today() - timedelta(days=_PET_LAG_DAYS)
    evap = fetch_station_pet(gauge_id, lat, lon, start, end)
    if evap.empty:
        return None

    return q, prec, evap
