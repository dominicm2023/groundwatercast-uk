"""Hardened cdsapi client construction — shared by every CDS fetch path.

Two CDS wedges in two days (refresh_seasonal_inputs overnight 2026-07-07,
refresh_pet evening 2026-07-08) had the same signature: a worker blocked
silently on a socket for hours (~0% CPU, no log lines, no retry messages), so
the callers' per-point retry/skip tolerance never fired — a hang raises
nothing. Each cost a multi-hour pipeline stall and a manual kill.

Hardening (defence in depth):

- ``socket.setdefaulttimeout(SOCKET_TIMEOUT_S)`` — the OS-level backstop: any
  socket operation that reaches the network without its own timeout now raises
  instead of blocking forever. Process-global by design: these are unattended
  batch processes, and every network call in them should fail loudly rather
  than hang cron. (Only set when no default exists, so an embedding process
  that chose its own policy is respected.)
- Bounded retry budget — the stock client retries ``retry_max=500`` times with
  ``sleep_max=120`` s sleeps: a 16-HOUR worst case per request even when the
  timeouts work. Trimmed so a genuinely-down CDS fails a point in ~10 minutes
  and the pipeline's existing per-point skip-tolerance moves on.
- kwargs applied through a fallback chain, so a future cdsapi with a different
  signature degrades to a stock client rather than crashing the chain (the
  same guard the old per-module ``_client()``s used for ``quiet``).
"""
from __future__ import annotations

import socket

SOCKET_TIMEOUT_S = 300      # backstop for any socket without its own timeout
REQUEST_TIMEOUT_S = 120     # per-HTTP-request timeout passed to the client
RETRY_MAX = 6               # ≈10 min of in-client retries, not 16 h
SLEEP_MAX = 60


def hardened_client():
    """A cdsapi.Client with timeouts + a sane retry budget.

    quiet=True mutes the per-poll INFO chatter ("Request ID is…", status
    updates, the ARCO boilerplate) that otherwise floods the cron logs;
    warnings and errors still surface.
    """
    import cdsapi  # lazy — only fetch paths need it (and only the main env has it)

    if socket.getdefaulttimeout() is None:
        socket.setdefaulttimeout(SOCKET_TIMEOUT_S)

    for kwargs in (
        dict(quiet=True, timeout=REQUEST_TIMEOUT_S,
             retry_max=RETRY_MAX, sleep_max=SLEEP_MAX),
        dict(quiet=True),
        dict(),
    ):
        try:
            return cdsapi.Client(**kwargs)
        except TypeError:
            continue
    raise RuntimeError("cdsapi.Client rejected every construction attempt")
