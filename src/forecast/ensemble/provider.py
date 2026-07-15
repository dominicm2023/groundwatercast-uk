"""Ensemble rainfall provider abstraction — the free→paid hinge.

The whole probabilistic-ensemble pipeline treats a weather ensemble as exactly
one thing: *N member daily-rainfall series for a location*. Every downstream
stage (bias-correction → bridge → Weibull recharge → GW roll → aggregation) is
provider-agnostic, so swapping the free source for a paid Met Office feed later
is one new class + one config value — no downstream change.

See docs/ensemble_forecast_design.md (§3) for the contract.

Output schema (tidy long form), one row per (member, date):
    member    : int   — 0 = control, 1..N = perturbed members
    date      : datetime64[ns] (UTC midnight, tz-naive) — one row per day
    precip_mm : float — daily total precipitation, non-negative

Raw provider payloads are cached under
    <cache_root>/<provider_name>/<run>/...
before any parsing, satisfying the raw-data-for-audit non-negotiable.
"""
from __future__ import annotations

import abc
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

OUTPUT_COLUMNS = ["member", "date", "precip_mm"]

# Cycle-cache dirs are named YYYYMMDDHH exactly (see _cache_dir) — anything
# else under <cache_root>/<name>/ is left alone by pruning.
_CYCLE_DIR_RE = re.compile(r"^\d{10}$")


class EnsembleRainfallProvider(abc.ABC):
    """Base class for ensemble rainfall providers.

    Subclasses implement :meth:`fetch`. They should call :meth:`_cache_dir`
    to obtain (and create) the per-run cache directory, write their raw
    payload there, then return a frame that passes :meth:`_validate`.
    """

    #: short, filesystem-safe identifier used in the cache path and config
    name: str = "base"

    def __init__(self, cache_root: str | Path = "data/raw/ensemble"):
        self.cache_root = Path(cache_root)

    # -- contract ------------------------------------------------------------
    @abc.abstractmethod
    def fetch(self, lat: float, lon: float, start: date,
              horizon_days: int) -> pd.DataFrame:
        """Return member daily rainfall for a point.

        Parameters
        ----------
        lat, lon      : point coordinates (WGS84 decimal degrees).
        start         : first forecast day requested (provider may begin at
                        its own run; callers filter to ``>= start`` as needed).
        horizon_days  : number of forecast days requested.

        Returns a DataFrame with columns OUTPUT_COLUMNS.
        """

    # -- shared helpers ------------------------------------------------------
    def _cache_dir(self, run: str) -> Path:
        """Return (and create) the cache dir for one run, e.g.
        ``data/raw/ensemble/open_meteo/2026060712``."""
        d = self.cache_root / self.name / run
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- cache retention -------------------------------------------------------
    def prune_old_cycles(self, retention_days: int) -> tuple[int, int]:
        """Delete this provider's cycle-cache dirs older than ``retention_days``.

        Cache layout (see :meth:`_cache_dir`): ``<cache_root>/<name>/<run>``,
        one dir per model cycle, ``run`` named ``YYYYMMDDHH``. Per-(run, steps)
        payloads are immutable, so once a cycle falls out of the retention
        window nothing downstream ever needs it again.

        Ages are parsed from the directory NAME, never the filesystem mtime —
        an mtime can be bumped by unrelated activity (a re-read, a backup
        walk, a clock skew) long after the cycle itself is stale, which would
        silently defeat retention. Anything that doesn't match the exact
        10-digit ``YYYYMMDDHH`` pattern, or doesn't parse as a real
        date/time, is left untouched — defensive, so pruning can never touch
        data this provider didn't create.

        ``retention_days <= 0`` disables pruning entirely (documented
        opt-out, e.g. for debugging a run against a full cache history).

        Returns ``(dirs_pruned, bytes_freed)``.
        """
        if retention_days <= 0:
            return (0, 0)
        provider_dir = self.cache_root / self.name
        if not provider_dir.is_dir():
            return (0, 0)
        now = datetime.now(timezone.utc)
        pruned, freed = 0, 0
        for entry in sorted(provider_dir.iterdir()):
            if not entry.is_dir() or not _CYCLE_DIR_RE.match(entry.name):
                continue
            try:
                run_dt = datetime.strptime(entry.name, "%Y%m%d%H").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue                    # malformed (e.g. month 13) — leave it
            age_days = (now - run_dt).total_seconds() / 86400.0
            if age_days < retention_days:
                continue
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            shutil.rmtree(entry)
            pruned += 1
            freed += size
        return (pruned, freed)

    def prune_old_cycles_safe(self, retention_days: int) -> tuple[int, int]:
        """:meth:`prune_old_cycles`, but a pruning failure only warns — it
        must never fail the fetch that triggered it. ``retention_days <= 0``
        (pruning disabled) is silent — there is nothing to report."""
        if retention_days <= 0:
            return (0, 0)
        try:
            pruned, freed = self.prune_old_cycles(retention_days)
        except Exception as exc:
            print(f"! ensemble cache prune failed ({type(exc).__name__}: {exc}) "
                  f"— leaving {self.cache_root / self.name} as-is")
            return (0, 0)
        print(f"pruned {pruned} ensemble cycle dir(s) older than {retention_days} "
              f"days (freed ~{freed / 1e6:.0f} MB)")
        return (pruned, freed)

    @staticmethod
    def _validate(df: pd.DataFrame) -> pd.DataFrame:
        """Normalise dtypes / ordering and assert the output contract."""
        missing = [c for c in OUTPUT_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"provider output missing columns: {missing}")
        out = df[OUTPUT_COLUMNS].copy()
        out["member"] = out["member"].astype(int)
        out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
        out["precip_mm"] = pd.to_numeric(out["precip_mm"], errors="coerce")
        # Daily precipitation is non-negative; clamp tiny negative artefacts.
        out["precip_mm"] = out["precip_mm"].clip(lower=0.0)
        out = (out.dropna(subset=["precip_mm"])
               .sort_values(["member", "date"])
               .reset_index(drop=True))
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_provider(name: str, *, cache_root: str | Path = "data/raw/ensemble",
                 **kwargs) -> EnsembleRainfallProvider:
    """Construct a provider by config name.

    Known names:
        "open_meteo"     — OpenMeteoEnsemble (free, non-commercial; prototyping)
        "ecmwf_opendata" — ECMWFOpenDataENS (free, CC-BY-4.0; production)
        "mogreps"        — reserved for the paid Met Office feed (not built)
    """
    # Local imports avoid importing heavy/optional deps (cfgrib) unless needed.
    if name == "open_meteo":
        from .open_meteo import OpenMeteoEnsemble
        return OpenMeteoEnsemble(cache_root=cache_root, **kwargs)
    if name == "ecmwf_opendata":
        from .ecmwf_opendata import ECMWFOpenDataENS
        return ECMWFOpenDataENS(cache_root=cache_root, **kwargs)
    if name == "mogreps":
        raise NotImplementedError(
            "The paid Met Office MOGREPS provider is a future drop-in "
            "(docs/ensemble_forecast_design.md §12). Not yet implemented."
        )
    raise ValueError(f"unknown ensemble provider: {name!r}")
