"""Tests for the hardened cdsapi construction (src/data/cds_client.py).

cdsapi isn't installed in the dev env, so a stub module stands in — which also
lets us simulate older/newer client signatures for the fallback chain.
"""
from __future__ import annotations

import socket
import sys
import types

import pytest

from src.data import cds_client as CC


@pytest.fixture(autouse=True)
def _restore_socket_default():
    prev = socket.getdefaulttimeout()
    yield
    socket.setdefaulttimeout(prev)


def _stub_cdsapi(monkeypatch, accepted_kwargs):
    """Install a fake cdsapi whose Client only accepts ``accepted_kwargs``."""
    mod = types.ModuleType("cdsapi")

    class Client:
        def __init__(self, **kwargs):
            unknown = set(kwargs) - set(accepted_kwargs)
            if unknown:
                raise TypeError(f"unexpected kwargs: {unknown}")
            self.kwargs = kwargs

    mod.Client = Client
    monkeypatch.setitem(sys.modules, "cdsapi", mod)
    return mod


def test_full_kwargs_and_socket_backstop(monkeypatch):
    _stub_cdsapi(monkeypatch, {"quiet", "timeout", "retry_max", "sleep_max"})
    socket.setdefaulttimeout(None)
    c = CC.hardened_client()
    assert c.kwargs == {"quiet": True, "timeout": CC.REQUEST_TIMEOUT_S,
                        "retry_max": CC.RETRY_MAX, "sleep_max": CC.SLEEP_MAX}
    # the OS-level backstop is installed…
    assert socket.getdefaulttimeout() == CC.SOCKET_TIMEOUT_S


def test_respects_preexisting_socket_policy(monkeypatch):
    _stub_cdsapi(monkeypatch, {"quiet", "timeout", "retry_max", "sleep_max"})
    socket.setdefaulttimeout(42.0)                    # embedding process chose one
    CC.hardened_client()
    assert socket.getdefaulttimeout() == 42.0         # …and it is respected


def test_falls_back_to_quiet_then_bare(monkeypatch):
    _stub_cdsapi(monkeypatch, {"quiet"})              # old client: quiet only
    assert CC.hardened_client().kwargs == {"quiet": True}
    _stub_cdsapi(monkeypatch, set())                  # ancient client: nothing
    assert CC.hardened_client().kwargs == {}


def test_retry_budget_is_minutes_not_hours():
    # The whole point: worst-case in-client retry time stays ~minutes.
    assert CC.RETRY_MAX * CC.SLEEP_MAX <= 600
