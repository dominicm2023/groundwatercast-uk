"""ECMWF Open Data ENS provider — the production source (CC-BY-4.0).

This is the commercial-clean free source locked in
docs/ensemble_forecast_design.md (§2, D1): 51-member IFS ENS, 15-day,
0.25° (~20 km), redistributable commercially with attribution.

Dependencies: ``ecmwf-opendata`` (retrieval) + ``cfgrib``/``eccodes``
(parsing) — see ``requirements-grib.txt``. pip eccodes ships binary wheels
(incl. Windows) since 2.37.0; on a CPython newer than the wheels (e.g.
3.14) use a small side-venv on the newest wheeled CPython — the
``.venv-grib`` pattern, mirroring ``.venv-pastas``. Imports are lazy and
raise a clear, actionable error when the stack is absent; for development
the ``open_meteo`` provider serves the same ECMWF ENS data as JSON.

Validated against the ``open_meteo`` provider by
``scripts/validate_ens_provider.py`` (W1 of docs/free_data_migration.md);
the parity report lives in ``outputs/ens_provider_parity.md``.

Member layout (IFS Cycle 50r1, Oct 2025+): the old in-stream ENS control
(enfo ``type=cf``) was discontinued — the former HRES is now the ENS
control and is disseminated in the ``oper`` stream (``type=fc``). Member 0
here is that control; members 1..50 are the enfo perturbed members.

Conventions (must match the provider contract / Open-Meteo):
  - ENS ``tp`` is accumulated (metres) from the forecast start. We request
    accumulations at **UTC-midnight step boundaries** and difference
    consecutive boundaries → full UTC-day totals in mm. For a 12Z run the
    0–12 h stub before the first midnight never becomes an increment.
  - Each daily increment is labelled with the UTC day it **covers**
    (window start) — Open-Meteo's labelling.
  - The cache is keyed on the actual model cycle (``YYYYMMDDHH`` resolved
    via ``Client.latest``), and per-run GRIBs are immutable → downloads
    are skipped when the files are already cached (one global download
    serves every borehole in a run).
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .provider import EnsembleRainfallProvider

# ENS open-data step grid: 3-hourly to 144 h, then 6-hourly to 360 h for
# the 00Z/12Z cycles; the 06Z/18Z cycles stop at 144 h. `latest` resolves
# with step=360, so the daily cron always lands on a full cycle; short
# cycles are still fetchable when pinned explicitly (the parity harness
# does — Open-Meteo mosaics them in).
_MAX_STEP_H = 360
_MAX_STEP_BY_HOUR = {0: 360, 6: 144, 12: 360, 18: 144}

_INSTALL_HINT = (
    "ECMWFOpenDataENS needs 'ecmwf-opendata' (retrieval) and 'cfgrib'+'eccodes' "
    "(GRIB parsing) — pip install -r requirements-grib.txt. Or use "
    "provider='open_meteo' for development (same ECMWF ENS data as JSON)."
)


def _require_parse_stack() -> None:
    """Raise an actionable ImportError when the GRIB parse stack is unusable.

    RuntimeError is caught too: the universal (py3-none-any) eccodes wheel
    installs fine but raises RuntimeError at import when no binary library
    exists for the interpreter (e.g. CPython newer than the wheels).
    """
    try:
        import xarray  # noqa: F401
        import cfgrib  # noqa: F401
    except (ImportError, RuntimeError) as exc:
        raise ImportError(_INSTALL_HINT) from exc


# ---------------------------------------------------------------------------
# Pure helpers (synthetically tested in tests/test_ecmwf_grib.py — no I/O)
# ---------------------------------------------------------------------------

def _utc_day_steps(base_hour: int, horizon_days: int,
                   max_step: int = _MAX_STEP_H) -> list[int]:
    """Forecast steps (hours from the run) landing on successive UTC-midnight
    boundaries, capped at the cycle's step limit.

    A 00Z run yields [0, 24, 48, …]; a 12Z run [12, 36, 60, …] (the 0–12 h
    stub before the first midnight is intentionally not a boundary). To get
    ``horizon_days`` daily increments we need ``horizon_days + 1`` boundaries,
    capped at ``max_step`` (360 h for 00/12Z, 144 h for 06/18Z). All
    boundaries land on the ENS step grid (multiples of 3 h to 144 h, of 6 h
    to 360 h).
    """
    first = (24 - int(base_hour)) % 24
    steps = [first + 24 * k for k in range(int(horizon_days) + 1)]
    return [s for s in steps if s <= max_step]


def _grid_lon(req_lon: float, ds_lons: np.ndarray) -> float:
    """Map a WGS84 longitude into the dataset's convention.

    cfgrib may decode the grid as 0..360 or ±180 — selecting 358.7° on a
    ±180 grid would silently snap to the antimeridian, so inspect the axis.
    """
    lons = np.asarray(ds_lons, dtype=float)
    if lons.max() > 180.0:                  # 0..360 grid
        return float(req_lon) % 360.0
    return ((float(req_lon) + 180.0) % 360.0) - 180.0


def _extract_point(ds, lat: float, lon: float):
    """Nearest-cell ``tp`` DataArray with a ``number`` dim.

    The control file (oper/fc — no ``number`` dim) is expanded to
    ``number=[0]``; pf files keep their member numbers (1..50).
    """
    pt = ds["tp"].sel(latitude=lat,
                      longitude=_grid_lon(lon, ds["longitude"].values),
                      method="nearest")
    if "number" not in pt.dims:
        pt = pt.expand_dims({"number": [0]})
    return pt


def _uk_subset(ds):
    """Decode + materialise just the UK region (a few MB) and drop the rest.

    The expensive step is cfgrib decoding the global grid; doing it once per run
    and keeping only the UK cells in memory means every borehole becomes a fast
    in-memory point lookup instead of a full re-decode. Handles both grid
    conventions (the UK straddles the meridian on a 0..360 grid, so select by
    index, not a slice)."""
    lons = np.asarray(ds["longitude"].values, dtype=float)
    lats = np.asarray(ds["latitude"].values, dtype=float)
    if lons.max() > 180.0:                       # 0..360 grid — UK wraps the meridian
        lon_idx = np.where((lons >= 350.0) | (lons <= 3.0))[0]
    else:                                        # ±180 grid
        lon_idx = np.where((lons >= -10.0) & (lons <= 3.0))[0]
    lat_idx = np.where((lats >= 49.0) & (lats <= 61.5))[0]
    return ds.isel(longitude=lon_idx, latitude=lat_idx).load()


def _daily_series(steps_h, tp_m, base_time: pd.Timestamp) -> pd.Series:
    """Accumulated tp (m) at UTC-midnight boundaries → daily totals (mm).

    Each increment between consecutive boundaries is labelled with the UTC
    day it COVERS (the window start — Open-Meteo's convention). Tiny
    negative diffs (GRIB packing noise) clamp to 0.
    """
    acc = pd.Series(np.asarray(tp_m, dtype=float),
                    index=np.asarray(steps_h, dtype=int)).sort_index()
    base = pd.Timestamp(base_time)
    base = base.tz_localize(None) if base.tzinfo else base
    days, vals = [], []
    steps = acc.index.to_list()
    for lo, hi in zip(steps[:-1], steps[1:]):
        inc = (acc.loc[hi] - acc.loc[lo]) * 1000.0     # m -> mm
        days.append((base + pd.Timedelta(hours=int(lo))).normalize())
        vals.append(max(float(inc), 0.0))
    return pd.Series(vals, index=pd.DatetimeIndex(days), name="precip_mm")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ECMWFOpenDataENS(EnsembleRainfallProvider):
    name = "ecmwf_opendata"

    def __init__(self, cache_root="data/raw/ensemble", *, source: str = "ecmwf",
                 run: str | None = None, cache_retention_days: int = 7):
        """``run`` pins a model cycle ("YYYYMMDDHH", 00/12Z); None = latest.
        Pinning is what makes like-for-like provider comparisons possible
        (scripts/validate_ens_provider.py) — the daily cron leaves it None.

        ``cache_retention_days`` (default 7, config `forecast.ensemble.
        cache_retention_days`) bounds the on-disk GRIB cycle cache: after
        each fresh download, cycle dirs older than this are pruned (see
        ``EnsembleRainfallProvider.prune_old_cycles``). Only the current
        cycle is ever read downstream, so a week of headroom is generous
        slack for retries/backfills while keeping the ~1 GB/day cache from
        growing unbounded. ``<= 0`` disables pruning."""
        super().__init__(cache_root)
        self.source = source
        self.run = run
        self.cache_retention_days = int(cache_retention_days)
        self._ds_cache: dict = {}      # per-run decoded UK grids (decode once, see _decoded)

    def fetch(self, lat: float, lon: float, start: date,
              horizon_days: int) -> pd.DataFrame:
        run_dt = self._resolve_run()
        steps = _utc_day_steps(run_dt.hour, int(horizon_days) + 1,
                               _MAX_STEP_BY_HOUR.get(run_dt.hour, _MAX_STEP_H))
        grib_paths = self._download(run_dt, steps)
        df = self._point_frame(self._decoded(grib_paths), lat, lon)
        df = df[df["date"] >= pd.Timestamp(start).normalize()]
        df = df[df["date"] < pd.Timestamp(start).normalize()
                + pd.Timedelta(days=int(horizon_days))]
        return self._validate(df)

    # -- retrieval (works without a GRIB reader) -----------------------------
    def _client(self):
        try:
            from ecmwf.opendata import Client
        except (ImportError, RuntimeError) as exc:  # pragma: no cover - env-dependent
            raise ImportError(_INSTALL_HINT) from exc
        return Client(source=self.source)

    def _resolve_run(self) -> datetime:
        """The model cycle this fetch uses. Pinned via ``self.run``, else the
        latest cycle that carries the FULL step range (step=360 excludes the
        144 h-only 06/18Z cycles)."""
        if self.run is not None:
            return datetime.strptime(self.run, "%Y%m%d%H")
        return self._client().latest(stream="enfo", type="pf", param="tp",
                                     step=_MAX_STEP_H)

    def _download(self, run_dt: datetime, steps: list[int]) -> list[Path]:
        cache = self._cache_dir(run_dt.strftime("%Y%m%d%H"))
        # The step range is in the filename so a short probe fetch and a
        # full-horizon fetch of the same run never collide.
        span = f"{steps[0]}h-{steps[-1]}h"
        paths = [cache / f"ens_tp_{kind}_{span}.grib2" for kind in ("ctrl", "pf")]
        if all(p.exists() and p.stat().st_size > 0 for p in paths):
            return paths                      # per-(run, steps) GRIBs are immutable

        client = self._client()
        # Since IFS Cycle 50r1 (Oct 2025) the old in-stream ENS control (enfo
        # type=cf) is gone: the former HRES *is* the ENS control, disseminated
        # as `oper` type=fc for ALL cycles (the 06/18Z `scda` stream was
        # retired with it — verified empirically against the dissemination
        # index, Jun 2026). Member 0 comes from there; the 50 perturbed
        # members stay in `enfo` type=pf.
        requests_ = (("oper", "fc", paths[0]), ("enfo", "pf", paths[1]))
        for stream, dtype, target in requests_:
            client.retrieve(
                date=run_dt.strftime("%Y-%m-%d"),
                time=run_dt.hour,
                stream=stream,
                type=dtype,
                param="tp",
                step=steps,
                target=str(target),
            )
        # Only prune after an actual download (not a cache hit above) — one
        # cycle appears per run, so this runs ~once/day in production rather
        # than once per borehole. A prune failure must never fail the fetch
        # that just succeeded.
        self.prune_old_cycles_safe(self.cache_retention_days)
        return paths

    # -- decode (needs cfgrib/eccodes) ---------------------------------------
    def _decoded(self, grib_paths: list[Path]) -> list:
        """Open + UK-subset + load the run's GRIBs ONCE, cached on the instance.

        The whole-grid cfgrib decode is the expensive step (~30 s/file); doing it
        inside the per-borehole parse made a national run O(N) in decodes (hours
        for 600+ boreholes). One provider instance serves every borehole in a
        run, so we decode once, keep only the UK region in memory (a few MB), and
        every fetch is then a fast point lookup. Keyed on the GRIB paths, which
        are per-(run, step-span) and immutable."""
        key = tuple(str(p) for p in grib_paths)
        cached = self._ds_cache.get(key)
        if cached is None:
            _require_parse_stack()
            import xarray as xr
            cached = []
            for path in grib_paths:
                ds = xr.open_dataset(str(path), engine="cfgrib",
                                     backend_kwargs={"indexpath": ""})
                cached.append(_uk_subset(ds))     # .load()s the small UK box
                ds.close()
            self._ds_cache[key] = cached
        return cached

    @staticmethod
    def _point_frame(datasets: list, lat: float, lon: float) -> pd.DataFrame:
        """Nearest-cell daily tp (mm) per member, from already-decoded datasets."""
        frames: list[dict] = []
        for ds in datasets:
            pt = _extract_point(ds, lat, lon)
            steps_h = (pd.to_timedelta(pt["step"].values)
                       / pd.Timedelta(hours=1)).astype(int)
            base_time = pd.Timestamp(ds["time"].values)
            for mi, member in enumerate(pt["number"].values.tolist()):
                daily = _daily_series(steps_h, pt.isel(number=mi).values,
                                      base_time)
                for day, mm in daily.items():
                    frames.append({"member": int(member), "date": day,
                                   "precip_mm": float(mm)})
        return pd.DataFrame(frames, columns=["member", "date", "precip_mm"])

    @staticmethod
    def _parse(grib_paths: list[Path], lat: float, lon: float) -> pd.DataFrame:
        """Open + extract one point, uncached. Retained for direct/test use;
        fetch() uses the per-run cached path (_decoded) for fleet-scale speed."""
        _require_parse_stack()
        import xarray as xr
        dsets = [xr.open_dataset(str(p), engine="cfgrib",
                                 backend_kwargs={"indexpath": ""}) for p in grib_paths]
        try:
            return ECMWFOpenDataENS._point_frame(dsets, lat, lon)
        finally:
            for ds in dsets:
                ds.close()
