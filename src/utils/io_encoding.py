"""UTF-8 stdio shim for non-TTY contexts on Windows.

On Windows, ``sys.stdout`` / ``sys.stderr`` default to cp1252 whenever
stdout is not a TTY (piped, redirected to a file, captured by a parent
process, run under Task Scheduler / a service). Any ``print()`` of an
em-dash, arrow, box-drawing char, ``≤``, ``…`` etc. then crashes with
``UnicodeEncodeError: 'charmap' codec can't encode character …``.

Call :func:`force_utf8_stdio` at the top of any script's ``main()``
entrypoint (or at module import time for scripts that print at module
level). It is idempotent and a no-op on POSIX, where stdout is already
UTF-8 by default.

This is intentionally a tiny standalone module with no third-party
imports so it can be called extremely early in any script's startup,
including from cron contexts where the environment is minimal.
"""
from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Reconfigure ``sys.stdout`` and ``sys.stderr`` to UTF-8.

    Safe to call multiple times. Silently no-ops on streams that don't
    support ``reconfigure`` (e.g. some test harnesses wrap stdout in
    objects that lack the method).
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Console-noise filter — suppress known-benign Windows + websockets messages
# ---------------------------------------------------------------------------

# Patterns that fire when a long-idle browser session drops on Windows
# (laptop sleep, network blip, tab closed without graceful disconnect).
# Streamlit catches the underlying OSError and cleans up correctly, but
# the websockets / asyncio stack prints full tracebacks to stderr before
# the cleanup runs. Disappears on Linux deployments — see
# docs/architecture.md "Console-noise filter" section.
_BENIGN_NOISE_PATTERNS = (
    "data transfer failed",                           # websockets stale-socket close
    "semaphore timeout period has expired",           # WinError 121
    "keepalive ping failed",                          # websockets ping timeout
    "Session with id .* is already connected",        # Streamlit re-connect notice
)


class _SilenceBenignNoise:
    """Logging filter that drops a small known-benign pattern list."""

    def __init__(self, patterns: tuple[str, ...]) -> None:
        import re
        self._patterns = [re.compile(p) for p in patterns]

    def filter(self, record) -> bool:
        msg = record.getMessage()
        return not any(p.search(msg) for p in self._patterns)


def silence_known_console_noise() -> None:
    """Install a logging filter on Windows-noisy loggers.

    Idempotent — adding the same filter object twice is a no-op for
    Python's logging library because filters are kept in a list and we
    check before appending.
    """
    import logging

    flt = _SilenceBenignNoise(_BENIGN_NOISE_PATTERNS)
    for name in (
        "websockets.legacy.protocol",
        "tornado.application",
        "asyncio",
        "streamlit.runtime.runtime",   # the "already connected" notice
    ):
        logger = logging.getLogger(name)
        # Avoid stacking duplicate filters on Streamlit hot-reloads
        if any(isinstance(f, _SilenceBenignNoise) for f in logger.filters):
            continue
        logger.addFilter(flt)
