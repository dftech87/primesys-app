from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    meuerp_base_url: str = "https://api.meuerponline.com.br/publica"
    session_secret: str = "dev-secret-change-me"
    database_path: str = "primesys.db"

    # Tenant seed (DFTECH) used by scripts/seed_tenant.py on first run.
    meuerp_cnpj: str | None = None
    meuerp_token: str | None = None

    # Protege a área /admin onde novos clientes (CNPJ + token da API) são cadastrados.
    admin_user: str = "admin"
    admin_password: str = "1608"


settings = Settings()
