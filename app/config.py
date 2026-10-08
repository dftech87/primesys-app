import logging
import os
import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    meuerp_base_url: str = "https://api.meuerponline.com.br/publica"
    session_secret: str = "dev-secret-change-me"
    # Chave que criptografa os tokens de API no banco (gerada com Fernet.generate_key(); fica só na variável de ambiente).
    token_encryption_key: str | None = None
    database_path: str = "primesys.db"

    # Tenant seed (DFTECH) used by scripts/seed_tenant.py on first run.
    meuerp_cnpj: str | None = None
    meuerp_token: str | None = None

    # Protege a área /admin onde novos clientes (CNPJ + token da API) são cadastrados.
    admin_user: str = "admin"
    admin_password: str = "1608"


settings = Settings()


# --- Valores padrão do código são públicos: nunca valem em produção. ---------------------------------------
# SESSION_SECRET padrão permitiria forjar cookies de login; sem a variável usamos um segredo aleatório por
# processo (todos precisam entrar de novo a cada reinício, mas ninguém consegue forjar sessão).
if settings.session_secret in {"", "dev-secret-change-me"}:
    settings.session_secret = secrets.token_urlsafe(48)
    logger.warning("SESSION_SECRET não definida: usando segredo aleatório temporário. Defina a variável.")

# A senha padrão do /admin (1608) está no código, então desativa o login até haver uma senha própria.
ADMIN_ATIVO = settings.admin_password not in {"", "1608"}
if not ADMIN_ATIVO:
    logger.warning("ADMIN_PASSWORD ausente ou padrão: o login do /admin está desativado. Defina uma senha forte.")

# Cookie de sessão só por HTTPS quando está na hospedagem (Railway define RAILWAY_ENVIRONMENT) ou se pedido.
COOKIE_SEGURO = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("COOKIE_SEGURO"))
SESSAO_MAX_IDADE = 7 * 24 * 3600   # re-login com a senha do ERP a cada 7 dias
