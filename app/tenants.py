import sqlite3

import httpx

from app.database import db_session
from app.meuerp_client import MeuERPClient
from app.security import normalize_cnpj


def create_tenant(cnpj: str, nome_fantasia: str, api_token: str) -> None:
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO empresas (cnpj, nome_fantasia, api_token)
            VALUES (?, ?, ?)
            ON CONFLICT(cnpj) DO UPDATE SET
                nome_fantasia = excluded.nome_fantasia,
                api_token = excluded.api_token
            """,
            (normalize_cnpj(cnpj), nome_fantasia, api_token),
        )


def list_tenants() -> list[sqlite3.Row]:
    with db_session() as conn:
        return conn.execute(
            "SELECT id, cnpj, nome_fantasia, ativo, criado_em, api_token FROM empresas ORDER BY criado_em DESC"
        ).fetchall()


def get_tenant_by_cnpj(cnpj: str) -> sqlite3.Row | None:
    with db_session() as conn:
        return conn.execute(
            "SELECT * FROM empresas WHERE cnpj = ? AND ativo = 1",
            (normalize_cnpj(cnpj),),
        ).fetchone()


async def authenticate(cnpj: str, email: str, senha: str) -> tuple[sqlite3.Row | None, str | None]:
    """Valida o login do cliente final contra o próprio usuário/senha do Meu ERP Online.

    Não guardamos senha nenhuma aqui: o token cadastrado no /admin só serve para
    consultar dados e para repassar a validação de credenciais à API do cliente.
    """
    tenant = get_tenant_by_cnpj(cnpj)
    if tenant is None:
        return None, "Empresa não encontrada. Verifique o CNPJ ou fale com o suporte."

    client = MeuERPClient(tenant["api_token"])
    try:
        resultado = await client.post("/api/usuario/validar/v1", json={"email": email, "senha": senha})
    except httpx.HTTPStatusError:
        return None, "Não foi possível validar suas credenciais agora. Tente novamente."

    if not resultado.get("status"):
        return None, resultado.get("mensagem") or "E-mail e/ou senha inválidos."

    return tenant, None
