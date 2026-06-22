"""Spliced ensemble: daily ENS to day ``splice_day``, EC46 beyond.

The 46-day extended forecast keeps the higher-resolution daily ENS (0.25°,
the operational 14-day driver) for its full skilful range and continues each
member on the corresponding EC46 extended-range member (36 km, 51 members,
46 days). Pairing is by member index: ENS member *m* continues on EC46
member *m*. The two are independent model states, so a member's day-15→16
transition is discontinuous — acceptable forcing noise for groundwater: the
Weibull recharge kernel (λ ≈ 10 d) smooths harder than the seam distorts,
and only the cross-member envelope carries signal at those leads anyway.

Failure posture: the extension is an enhancement. If the EC46 fetch fails,
degrade LOUDLY to the primary's days (a 14-day forecast today beats no
forecast), never block the daily build on the extended tail.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from .provider import EnsembleRainfallProvider


class SplicedEnsemble(EnsembleRainfallProvider):
    """Composite provider: ``primary`` days 1..splice_day, ``extension`` after."""

    def __init__(self, primary: EnsembleRainfallProvider,
                 extension: EnsembleRainfallProvider, *,
                 splice_day: int = 15):
        # No own cache dir — the delegates cache their raw payloads themselves.
        super().__init__(primary.cache_root)
        self.primary = primary
        self.extension = extension
        self.splice_day = int(splice_day)
        self.name = f"{primary.name}+{extension.name}"

    def fetch(self, lat: float, lon: float, start: date,
              horizon_days: int) -> pd.DataFrame:
        head = self.primary.fetch(lat, lon, start,
                                  min(int(horizon_days), self.splice_day))
        if int(horizon_days) <= self.splice_day:
            return self._validate(head)

        try:
            tail = self.extension.fetch(lat, lon, start, int(horizon_days))
        except Exception as exc:
            print(f"[splice] WARNING: extension provider "
                  f"{self.extension.name!r} failed ({exc}) — degrading to "
                  f"{self.primary.name!r} days only "
                  f"(<= day {self.splice_day}).")
            return self._validate(head)

        if head.empty:
            # Primary returned no data for the skilful operational window. The
            # failure posture is to degrade LOUDLY (never silently serve the
            # low-skill extension over days 1-15 as if it were the primary).
            print(f"[splice] WARNING: primary provider {self.primary.name!r} "
                  f"returned no data for the skilful days 1-{self.splice_day} — "
                  f"serving the {self.extension.name!r} extended range alone "
                  f"(low daily skill over the operational window). "
                  f"Investigate the primary feed.")
            return self._validate(tail)
        cut = head["date"].max()                     # last day the primary covers
        tail = tail[tail["date"] > cut]
        # Pair by member index; an extension member with no primary
        # counterpart is dropped (a tail with no head would fabricate a
        # member that never had skilful days 1-15).
        tail = tail[tail["member"].isin(head["member"].unique())]
        spliced = pd.concat([head, tail], ignore_index=True)
        return self._validate(spliced)
