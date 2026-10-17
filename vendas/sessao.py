"""Acesso de cada celular ao servidor do Prime Vendas.

No login (e-mail e senha do ERP) o servidor gera um token aleatório e entrega ao aparelho, que o guarda
protegido pelo PIN. Aqui fica só o hash SHA-256 dele: o banco sozinho não permite se passar por ninguém.
Toda chamada do app manda o token no cabeçalho Authorization; revogar (desconectar o celular) vale na hora.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from fastapi import Header, HTTPException

from app.database import db_session
from app.tenants import get_tenant_by_cnpj
from vendas import config, vendedores
from vendas.db import agora_texto, texto_para_data

logger = logging.getLogger(__name__)

# Gravar "último uso" a cada chamada pesaria no banco; basta atualizar de tempos em tempos.
ATUALIZAR_ULTIMO_USO_A_CADA = timedelta(minutes=10)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def criar(cnpj: str, vendedor: dict, aparelho: str) -> str:
    token = secrets.token_urlsafe(32)
    agora = agora_texto()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO pv_aparelhos (cnpj, token_hash, id_usuario, id_funcionario, nome_vendedor, aparelho,
                                         criado_em, ultimo_uso, conferido_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cnpj, _hash(token), vendedor["id_usuario"], vendedor["id_funcionario"], vendedor["nome"], aparelho[:120],
             agora, agora, agora),
        )
    return token


def revogar(token: str) -> None:
    with db_session() as conn:
        conn.execute("UPDATE pv_aparelhos SET revogado = 1 WHERE token_hash = ?", (_hash(token),))


def _revogar_id(id_aparelho: int) -> None:
    with db_session() as conn:
        conn.execute("UPDATE pv_aparelhos SET revogado = 1 WHERE id = ?", (id_aparelho,))


def empresas_com_aparelhos() -> list[str]:
    """Empresas com algum celular conectado (não desconectado) usado nos últimos 30 dias."""
    limite = (texto_para_data(agora_texto()) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as conn:
        return [r["cnpj"] for r in conn.execute("SELECT DISTINCT cnpj FROM pv_aparelhos WHERE revogado = 0 AND ultimo_uso >= ?", (limite,))]


NAO_AUTORIZADO = HTTPException(status_code=401, detail="Acesso expirado. Entre de novo com seu e-mail e senha do ERP.")


async def aparelho_atual(authorization: str = Header(default="")) -> dict:
    """Dependência das rotas do app: aparelho válido, empresa ativa e liberada, vendedor ainda vinculado.
    Devolve o aparelho junto com a empresa (token do ERP aberto só em memória)."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise NAO_AUTORIZADO
    with db_session() as conn:
        linha = conn.execute("SELECT * FROM pv_aparelhos WHERE token_hash = ? AND revogado = 0", (_hash(token),)).fetchone()
    if linha is None:
        raise NAO_AUTORIZADO
    aparelho = dict(linha)
    agora = texto_para_data(agora_texto())
    if agora - texto_para_data(aparelho["ultimo_uso"]) > timedelta(days=config.APARELHO_VENCE_DIAS):
        _revogar_id(aparelho["id"])
        raise NAO_AUTORIZADO
    tenant = get_tenant_by_cnpj(aparelho["cnpj"])
    if tenant is None:   # empresa desativada no /admin
        raise NAO_AUTORIZADO
    if not config.empresa_liberada(tenant):   # Prime Vendas bloqueado no /admin: desconecta na hora
        raise HTTPException(status_code=403, detail="O Prime Vendas não está liberado para esta empresa.")

    mudancas = {}
    if agora - texto_para_data(aparelho["conferido_em"]) > timedelta(hours=config.RECONFERIR_VENDEDOR_HORAS):
        ativo = await vendedores.ainda_ativo(tenant, aparelho["id_usuario"], aparelho["id_funcionario"])
        if ativo is False:
            logger.warning("vendedor %s da empresa %s não está mais ativo/vinculado: aparelho desconectado",
                           aparelho["id_funcionario"], aparelho["cnpj"])
            _revogar_id(aparelho["id"])
            raise NAO_AUTORIZADO
        if ativo:   # None = ERP não respondeu: tenta de novo na próxima chamada
            mudancas["conferido_em"] = agora_texto()
    if agora - texto_para_data(aparelho["ultimo_uso"]) > ATUALIZAR_ULTIMO_USO_A_CADA:
        mudancas["ultimo_uso"] = agora_texto()
    if mudancas:
        with db_session() as conn:
            for coluna, valor in mudancas.items():   # nomes de coluna fixos, definidos acima
                conn.execute(f"UPDATE pv_aparelhos SET {coluna} = ? WHERE id = ?", (valor, aparelho["id"]))
    aparelho["tenant"] = tenant
    return aparelho
