"""Backward-compatible, secure Angel One module.

Secrets are read exclusively from environment variables. Never commit broker
credentials or expose them in MCP output, logs, or configuration files.
"""

from brokers.angelone import AngelOneBroker, AngelOneCredentials

_client = None


def create_client() -> AngelOneBroker:
    """Return the process-local, lazily authenticated read-only client."""
    global _client
    if _client is None:
        _client = AngelOneBroker(AngelOneCredentials.from_environment())
    return _client


class AngelOneApi(AngelOneBroker):
    """Compatibility alias for the original integration class."""

    def __init__(self, api_key: str, client_id: str, pin: str, totp_secret: str):
        super().__init__(AngelOneCredentials(api_key, client_id, pin, totp_secret))