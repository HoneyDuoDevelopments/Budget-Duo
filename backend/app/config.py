from pydantic_settings import BaseSettings
from urllib.parse import quote_plus

class Settings(BaseSettings):
    database_url: str = ""
    secret_key: str
    teller_cert_path: str = "/app/certs/certificate.pem"
    teller_key_path: str = "/app/certs/private_key.pem"
    teller_env: str = "development"
    teller_base_url: str = "https://api.teller.io"

    # Postgres connection components — used to build URL safely
    postgres_password: str = ""

    @property
    def safe_database_url(self) -> str:
        encoded = quote_plus(self.postgres_password)
        return f"postgresql://budget_duo:{encoded}@db:5432/budget_duo"

    ACCOUNT_ALIASES: dict = {
        "acc_ppu17g8ubisnfdkj2q001": "acc_ppuaa3d9jaqul1qusa000",
        "acc_ppu17g8rrisnfdkj2q000": "acc_ppuaa3d73aqul1qusa000",
    }

    BILLS_ONLY_ACCOUNTS: list = [
        "acc_ppuaa3d73aqul1qusa000",
    ]

    SAVINGS_ACCOUNTS: list = [
        "acc_ppu17g8trisnfdkj2q001",
        "acc_ppuaa3d53aqul1qusa000",
        "acc_ppu9pf8ejaqul1qusa000",
    ]

    class Config:
        env_file = ".env"

settings = Settings()