import httpx
from app.config import settings


class TellerClient:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = settings.teller_base_url
        self.cert = (settings.teller_cert_path, settings.teller_key_path)

    def _client(self):
        return httpx.Client(
            cert=self.cert,
            auth=(self.access_token, ""),
            timeout=30.0,
        )

    def get_accounts(self):
        with self._client() as client:
            r = client.get(f"{self.base_url}/accounts")
            r.raise_for_status()
            return r.json()

    def get_transactions(self, account_id: str, from_date: str = None):
        params = {}
        if from_date:
            params["from_date"] = from_date
        with self._client() as client:
            r = client.get(
                f"{self.base_url}/accounts/{account_id}/transactions",
                params=params,
            )
            r.raise_for_status()
            return r.json()

    def get_balance(self, account_id: str):
        with self._client() as client:
            r = client.get(
                f"{self.base_url}/accounts/{account_id}/balances"
            )
            r.raise_for_status()
            return r.json()