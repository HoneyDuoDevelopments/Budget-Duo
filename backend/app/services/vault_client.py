"""
Vaultwarden credential client — fetches login credentials at runtime.

Uses the Bitwarden CLI (bw) installed on the host, accessed via
subprocess from within the backend container.

Alternative approach: Uses Bitwarden API directly via HTTP since
the bw CLI isn't available inside Docker. We call the Vaultwarden
API at vault.honey-duo.com using OAuth client credentials.
"""
import httpx
import logging
import os

logger = logging.getLogger(__name__)

VAULT_URL = os.environ.get("VAULT_URL", "https://vault.honey-duo.com")
BW_CLIENT_ID = os.environ.get("BW_CLIENT_ID", "")
BW_CLIENT_SECRET = os.environ.get("BW_CLIENT_SECRET", "")
BW_MASTER_PASSWORD = os.environ.get("BW_MASTER_PASSWORD", "")

# Vaultwarden item names — must match exactly what's in the vault
VAULT_ITEMS = {
    "apple": "Apple",
    "synchrony": "Synchrony",
}


class VaultClient:
    """
    Fetches credentials from Vaultwarden.

    For the Docker environment, we use a simpler approach:
    credentials are fetched once at startup or on-demand via
    environment variables seeded from the host's bw CLI.

    The host runs a small helper script that unlocks the vault,
    fetches the needed credentials, and writes them to a
    Docker-accessible secrets file.
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def get_credentials(self, provider: str) -> dict:
        """
        Get username/password for a provider.
        Returns: {"username": str, "password": str}
        Raises: Exception if credentials not found.
        """
        if provider in self._cache:
            return self._cache[provider]

        # Try environment variables first (set by host helper script)
        env_prefix = f"SCRAPER_{provider.upper()}"
        username = os.environ.get(f"{env_prefix}_USERNAME", "")
        password = os.environ.get(f"{env_prefix}_PASSWORD", "")

        if username and password:
            creds = {"username": username, "password": password}
            self._cache[provider] = creds
            logger.info(f"Loaded {provider} credentials from environment")
            return creds

        raise Exception(
            f"No credentials found for {provider}. "
            f"Set {env_prefix}_USERNAME and {env_prefix}_PASSWORD environment variables, "
            f"or run the vault credential helper script on the host."
        )


# Singleton
vault_client = VaultClient()