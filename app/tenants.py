import logging
import sqlite3

import httpx

from app import cripto
from app.cache import cached
from app.database import db_session, get_connection
from app.meuerp_client import MeuERPClient
from app.security import normalize_cnpj

logger = logging.getLogger(__name__)

ERRO_INDISPONIVEL = "Não foi possível validar suas credenciais agora. Tente novamente."
ERRO_TOKEN_DE_OUTRA_EMPRESA = "O cadastro desta empresa está com a chave de acesso de outra empresa. Fale com o suporte."
TTL_DONO_DO_TOKEN = 6 * 3600


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
            (normalize_cnpj(cnpj), nome_fantasia, cripto.proteger(api_token)),
        )


def create_tenant_if_missing(cnpj: str, nome_fantasia: str, api_token: str) -> None:
    """Cadastro inicial (variáveis de ambiente): só cria se o CNPJ ainda não existe. Nunca sobrescreve o token que
    foi cadastrado ou corrigido pelo /admin."""
    with db_session() as conn:
        conn.execute(
            "INSERT INTO empresas (cnpj, nome_fantasia, api_token) VALUES (?, ?, ?) ON CONFLICT(cnpj) DO NOTHING",
            (normalize_cnpj(cnpj), nome_fantasia, cripto.proteger(api_token)),
        )


async def empresa_do_token(token: str) -> str | None:
    """CNPJ (só dígitos) da empresa dona do token, segundo o próprio ERP. None se não deu para saber."""
    try:
        resposta = await MeuERPClient(token).get("/api/empresa/v1")
    except Exception:
        return None
    return normalize_cnpj(resposta.get("cnpjCpf")) or None


async def token_confere_com_cnpj(tenant: dict) -> bool:
    """False só quando o ERP afirma que o token é de OUTRA empresa. Se não deu para conferir (ERP fora do ar),
    não bloqueia. Só o resultado 'confere' fica guardado, para uma correção no /admin valer na hora."""
    dono = await cached(
        (tenant["cnpj"], "dono-do-token"),
        TTL_DONO_DO_TOKEN,
        lambda: empresa_do_token(tenant["api_token"]),
        guardar_se=lambda v: v == tenant["cnpj"],
    )
    return dono is None or dono == tenant["cnpj"]


def _com_token_aberto(linha: sqlite3.Row) -> dict:
    """Linha do banco com o token já descriptografado (só em memória, para chamar o ERP)."""
    dados = dict(linha)
    if "api_token" in dados:
        dados["api_token"] = cripto.revelar(dados["api_token"])
    return dados


def list_tenants() -> list[dict]:
    with db_session() as conn:
        linhas = conn.execute(
            "SELECT id, cnpj, nome_fantasia, ativo, criado_em, api_token FROM empresas ORDER BY criado_em DESC"
        ).fetchall()
    resultado = []
    for linha in linhas:
        try:
            resultado.append(_com_token_aberto(linha))
        except cripto.CriptoErro:
            logger.error("token ilegível para o CNPJ %s (chave de criptografia ausente ou diferente)", linha["cnpj"])
            resultado.append({**dict(linha), "api_token": ""})
    return resultado


def set_tenant_ativo(cnpj: str, ativo: bool) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE empresas SET ativo = ? WHERE cnpj = ?",
            (1 if ativo else 0, normalize_cnpj(cnpj)),
        )


def get_tenant_by_cnpj(cnpj: str) -> dict | None:
    """Empresa ativa com o token já aberto. Se o token não puder ser lido (chave ausente/errada), devolve None
    e registra o erro: o cliente cai na tela de login em vez de receber um erro do ERP."""
    with db_session() as conn:
        linha = conn.execute(
            "SELECT * FROM empresas WHERE cnpj = ? AND ativo = 1",
            (normalize_cnpj(cnpj),),
        ).fetchone()
    if linha is None:
        return None
    try:
        return _com_token_aberto(linha)
    except cripto.CriptoErro:
        logger.error("token ilegível para o CNPJ %s (chave de criptografia ausente ou diferente)", linha["cnpj"])
        return None


def criptografar_tokens_existentes() -> int:
    """Converte para criptografado os tokens ainda em texto simples (precisa de TOKEN_ENCRYPTION_KEY).

    Tudo numa única transação e cada token é conferido (descriptografa e compara) antes de gravar: se algo
    não bater, nada muda. Depois roda VACUUM para apagar do arquivo os restos do texto simples antigo.
    Idempotente: sem tokens em texto simples, não faz nada."""
    if not cripto.ativa():
        return 0
    conn = get_connection()
    try:
        pendentes = [l for l in conn.execute("SELECT id, api_token FROM empresas") if not cripto.protegido(l["api_token"])]
        if not pendentes:
            return 0
        for linha in pendentes:
            novo = cripto.proteger(linha["api_token"])
            if cripto.revelar(novo) != linha["api_token"]:
                raise RuntimeError("conferência da criptografia falhou; nenhum token foi alterado")
            conn.execute("UPDATE empresas SET api_token = ? WHERE id = ?", (novo, linha["id"]))
        conn.commit()
        conn.execute("VACUUM")
        logger.warning("%d token(s) de API passaram a ser gravados criptografados.", len(pendentes))
        return len(pendentes)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def authenticate(cnpj: str, email: str, senha: str) -> tuple[dict | None, str | None]:
    """Valida o login do cliente final contra o próprio usuário/senha do Meu ERP Online.

    Não guardamos senha nenhuma aqui: o token cadastrado no /admin só serve para
    consultar dados e para repassar a validação de credenciais à API do cliente.
    """
    try:
        tenant = get_tenant_by_cnpj(cnpj)
    except Exception:
        logger.exception("falha ao ler a empresa no banco")
        return None, ERRO_INDISPONIVEL
    if tenant is None:
        return None, "Empresa não encontrada. Verifique o CNPJ ou fale com o suporte."

    # Garante que o token cadastrado é mesmo desta empresa: um token trocado entregaria os dados de outra empresa.
    if not await token_confere_com_cnpj(tenant):
        logger.error("TOKEN DE OUTRA EMPRESA no cadastro do CNPJ %s: acesso bloqueado até corrigir no /admin", tenant["cnpj"])
        return None, ERRO_TOKEN_DE_OUTRA_EMPRESA

    client = MeuERPClient(tenant["api_token"])
    try:
        resultado = await client.post("/api/usuario/validar/v1", json={"email": email, "senha": senha})
    except httpx.HTTPStatusError:
        return None, ERRO_INDISPONIVEL

    if not resultado.get("status"):
        return None, resultado.get("mensagem") or "E-mail e/ou senha inválidos."

    return tenant, None
