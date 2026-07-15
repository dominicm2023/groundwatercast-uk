"""
Flow-gauge catalogue + links builder — Stage 1 of the low-flow Rivers layer
build (``docs/product/lowflow/build_plan.md``).

Flow gauges are first-class stations (build_plan.md architecture decision 1),
not attributes of boreholes: their own catalogue, their own links table.

Downloads EA Hydrology stations with ``observedProperty=waterFlow``, keeps
the daily-mean qualified flow measure (``<guid>-flow-m-86400-m3s-qualified``)
for every OPEN gauge, and writes ``data/processed/flow_catalogue.csv``.

``flow_links.csv`` links each gauge to its 3 nearest EA rainfall gauges,
selected with the SAME nearest-3 machinery ``src.linking.build`` uses for
boreholes (``haversine`` / ``nearest_n``, reused unchanged — no bespoke
distance code here), against the SAME national rainfall rows already
selected for ``station_links.csv`` (``src.linking.build.select_predictors_rainfall``
over ``data/processed/catalogue.csv``). Columns/semantics match
``station_links.csv``'s rain-linking columns exactly
(``RainMeasureID_1..3`` / ``RainDist_1..3``) so
``gauge_rainfall_for`` (``src/forecast/ensemble/members.py``) can consume
``flow_links.csv`` with only a links-frame swap — it only ever does
``links.loc[sid]`` plus ``get("RainMeasureID_{i}")``, so the exact index
column name doesn't matter to it (this module uses ``GaugeID``).

No separate PET link column is needed: PET is fetched directly at a
station's own ``(lat, lon)`` (``src.data.pet.fetch_station_pet``), exactly as
it already is for boreholes — a flow gauge's own ``flow_catalogue.csv``
lat/lon *is* its PET point, nothing to link.

The broken-rain-gauge screen (mean > 12 mm/day excluded as a broken/
cumulative series) lives in
``src.forecast.ensemble.members.observed_daily_rainfall`` and is reused
as-is by any caller that hands it ``RainMeasureID_1..3`` values — including
ones sourced from ``flow_links.csv`` — with no changes needed here.

Usage:
    python -m scripts.build_flow_catalogue
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from src.linking.build import nearest_n, select_predictors_rainfall

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    config_path = Path(__file__).parents[2] / "config" / "config.json"
    with open(config_path) as f:
        return json.load(f)


_DEFAULT_FLOW_CFG = {
    "stations_url": "https://environment.data.gov.uk/hydrology/id/stations.csv",
    "observed_property": "waterFlow",
    "stations_limit": 20000,
    "output_path": "data/processed/flow_catalogue.csv",
    "links_output_path": "data/processed/flow_links.csv",
}


def flow_catalogue_config(config: dict) -> dict:
    """``config['flow_catalogue']`` with defaults filled in for any missing key."""
    return {**_DEFAULT_FLOW_CFG, **(config.get("flow_catalogue") or {})}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_flow_stations(url: str, *, observed_property: str = "waterFlow",
                        limit: int = 20000) -> pd.DataFrame:
    """Download the EA Hydrology stations CSV filtered to one observedProperty."""
    response = requests.get(
        url,
        params={"observedProperty": observed_property, "_limit": limit},
        timeout=60,
    )
    response.raise_for_status()
    return pd.read_csv(StringIO(response.text))


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

_DAILY_MEAN_SUFFIX = "-flow-m-86400-m3s-qualified"


def _split_pipe(val) -> list[str]:
    if pd.isna(val) or str(val).strip() == "":
        return []
    return [s.strip() for s in str(val).split("|") if s.strip()]


def daily_mean_measure_id(measures_raw) -> str | None:
    """Pick the ``<guid>-flow-m-86400-m3s-qualified`` measure id out of a
    station's pipe-delimited ``measures`` URI list. ``None`` if the station
    (unusually) does not carry a daily-mean *qualified* flow measure."""
    for uri in _split_pipe(measures_raw):
        mid = uri.rstrip("/").split("/")[-1]
        if mid.endswith(_DAILY_MEAN_SUFFIX):
            return mid
    return None


def is_open(row: pd.Series) -> bool:
    """A gauge counts as 'open' when it has no ``dateClosed`` and its status
    label does not include 'Closed'.

    Suspended gauges (flow/level telemetry paused) still count as open —
    they remain a real, catalogued station; a live data-quality caveat is a
    downstream (ingest/daily-refresh) concern, not a Stage-1 catalogue one.
    """
    date_closed = row.get("dateClosed")
    if pd.notna(date_closed) and str(date_closed).strip():
        return False
    label = str(row.get("status.label") or "")
    return "Closed" not in label


_CATALOGUE_RENAME = {
    "label": "station_name",
    "riverName": "river_name",
    "catchmentName": "catchment_name",
    "dateOpened": "record_start",
}


def parse_flow_catalogue(raw: pd.DataFrame) -> pd.DataFrame:
    """Filter to open gauges carrying the daily-mean qualified flow measure
    and extract the Stage-1 catalogue columns.

    Output columns: station_id, station_name, lat, lon, river_name,
    catchment_name, flow_measure_id, record_start.
    """
    df = raw.copy()
    df["flow_measure_id"] = df["measures"].apply(daily_mean_measure_id)
    df = df[df["flow_measure_id"].notna()]
    df = df[df.apply(is_open, axis=1)]

    # station_id = the flow_measure_id with the daily-mean-qualified suffix
    # stripped, NOT the bare stationGuid. The EA API reuses one stationGuid
    # across colocated split-flow channels (e.g. "Coolham Total" / "Main" /
    # "Side", "Denham Lodge" / "Denham Lodge Side") — live-verified
    # 2026-07-14: 19 stationGuids cover 2-3 distinct gauges each. Each
    # distinct channel carries its own compound measure id
    # (``<guid>_<suffix>-flow-m-86400-m3s-qualified``), which is exactly the
    # id shape docs/product/lowflow/scripts/validation_fetch.py's
    # Medway@Teston row already uses
    # (``eba748a3-...-a671-5ef94b896ffa_453202901``). Deriving station_id
    # this way keeps plain single-channel gauges unchanged (station_id ==
    # stationGuid) while giving every real distinct channel its own row.
    df["station_id"] = df["flow_measure_id"].str.removesuffix(_DAILY_MEAN_SUFFIX)
    bad = df["station_id"].isna() | (df["station_id"].astype(str).str.strip() == "")
    if bad.any():
        raise ValueError(
            f"{int(bad.sum())} open flow-measure rows have no derivable station_id"
        )

    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["long"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])

    keep = ["station_id", "label", "lat", "lon", "riverName", "catchmentName",
            "flow_measure_id", "dateOpened"]
    for col in keep:
        if col not in df.columns:
            df[col] = None

    out = df[keep].rename(columns=_CATALOGUE_RENAME)
    out = out.drop_duplicates("station_id").sort_values("station_id")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Links (rain gauge nearest-3, reusing src.linking.build unchanged)
# ---------------------------------------------------------------------------


def build_flow_links(flow_catalogue: pd.DataFrame,
                     rain_candidates: pd.DataFrame, *, n: int = 3) -> pd.DataFrame:
    """One output row per flow gauge with its ``n`` nearest rainfall links.

    ``rain_candidates`` must carry ``lat``/``lon``/``measure_id`` — the
    shape ``select_predictors_rainfall`` returns. Column names/semantics for
    the rain-link columns match ``station_links.csv`` exactly
    (``RainMeasureID_1..3`` / ``RainDist_1..3``); ``nearest_n`` is the
    unchanged borehole-linking selector.
    """
    rows = []
    for _, gauge in flow_catalogue.iterrows():
        if pd.isna(gauge.get("station_id")) or str(gauge["station_id"]).strip() == "":
            continue
        rain = nearest_n(gauge, rain_candidates, n=n)
        row = {
            "GaugeID": gauge["station_id"],
            "FlowMeasureID": gauge.get("flow_measure_id"),
        }
        for i in range(n):
            row[f"RainMeasureID_{i + 1}"] = rain[i][0]
        for i in range(n):
            row[f"RainDist_{i + 1}"] = rain[i][1]
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------


def build_flow_catalogue(config: dict) -> pd.DataFrame:
    """Fetch + parse + write ``flow_catalogue.csv``. Returns the catalogue df."""
    flow_cfg = flow_catalogue_config(config)
    output_path = Path(__file__).parents[2] / flow_cfg["output_path"]

    print(f"Fetching flow stations (observedProperty="
          f"{flow_cfg['observed_property']})...")
    raw = fetch_flow_stations(
        flow_cfg["stations_url"],
        observed_property=flow_cfg["observed_property"],
        limit=flow_cfg["stations_limit"],
    )
    print(f"  {len(raw)} stations downloaded")

    catalogue = parse_flow_catalogue(raw)
    print(f"  {len(catalogue)} open gauges with a daily-mean qualified flow measure")

    record_start = pd.to_datetime(catalogue["record_start"], errors="coerce")
    n_pre_1990 = int((record_start < pd.Timestamp("1990-01-01")).sum())
    print(f"  {n_pre_1990} gauges opened pre-1990")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    catalogue.to_csv(output_path, index=False)
    print(f"flow_catalogue.csv written to {output_path}")

    return catalogue


def build_flow_links_from_config(config: dict,
                                 flow_catalogue: pd.DataFrame) -> pd.DataFrame:
    """Build + write ``flow_links.csv`` from the national rainfall catalogue
    (``config['catalogue']['output_path']``, the same rows
    ``src.linking.build`` selects rainfall predictors from for boreholes)."""
    flow_cfg = flow_catalogue_config(config)
    output_path = Path(__file__).parents[2] / flow_cfg["links_output_path"]

    catalogue_path = Path(__file__).parents[2] / config["catalogue"]["output_path"]
    national = pd.read_csv(catalogue_path)
    rain_prefs = config["linking"].get("rainfall_preference", {})
    rain_candidates = select_predictors_rainfall(national, rain_prefs)
    print(f"  {len(rain_candidates)} candidate rainfall gauges (national catalogue)")

    links = build_flow_links(flow_catalogue, rain_candidates)

    missing_rain = links[
        ["RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3"]
    ].isna().any(axis=1).sum()
    if missing_rain:
        print(f"  WARNING: {missing_rain} gauges have fewer than 3 rainfall links")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    links.to_csv(output_path, index=False)
    print(f"flow_links.csv written to {output_path}")

    return links
