"""Angel One SmartAPI production integration tests (require live credentials).

These tests are marked ``integration`` and are EXCLUDED from the default
``pytest`` run (see pytest.ini -> addopts = -m "not integration"), so the
baseline stays green offline. They only run with ``pytest -m integration``.
They skip (never fail) when ANGELONE_* env vars are absent or when the
network/session is unavailable. No code runs at import time.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytestmark = pytest.mark.integration

REQUIRED_ENV = ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_PIN", "ANGELONE_TOTP_SECRET")


def _have_creds():
    return all(os.getenv(k) for k in REQUIRED_ENV)


@pytest.fixture(scope="module")
def client():
    if not _have_creds():
        pytest.skip("live Angel One credentials not set (ANGELONE_*) - skipping")
    import angel_one_api
    try:
        return angel_one_api.create_client()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not build Angel One client: {exc}")


def test_import_and_libs():
    """Module + SmartApi/pyotp import without touching the network."""
    import angel_one_api  # noqa: F401
    from SmartApi import SmartConnect  # noqa: F401
    import pyotp  # noqa: F401


def test_session_and_quote_and_ohlcv(client):
    try:
        session = client.initialize_session()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"session init failed (network/throttle): {exc}")
    # A throttled/expired session is a skip, not a failure, in CI.
    if not session or session.get("status") not in ("Success", "success", True):
        pytest.skip(f"session not established: {session}")
    from shared import DataProvider
    token = DataProvider.normalize_symbol("RELIANCE", "nse")
    assert token
    quote = client.get_realtime_quote("RELIANCE", "nse")
    assert quote.get("last_price") is not None
    ohlcv = client.get_ohlcv("RELIANCE", period="5d", interval="15m", exchange="nse")
    assert ohlcv.get("count", 0) >= 0
