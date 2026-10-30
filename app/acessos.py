"""O que cada usuário vê no Prime Info: as abas e as informações da ficha seguem as permissões do perfil dele no Meu ERP.

O ERP não diz qual usuário acabou de validar o login, então o e-mail é procurado entre os usuários que têm perfil na
empresa (banco do ERP: usuario_perfil_empresa, a mesma regra de empresa do Prime Vendas), lendo o detalhe de cada um
só até achar. Quem não tem perfil na empresa (ex.: usuário de suporte/revenda) vê tudo, como antes dos perfis.

Os acessos calculados ficam na sessão (assinada); a página principal os recalcula a cada abertura, então uma mudança
de perfil no ERP vale em até 1 hora (validade das permissões guardadas). As rotas da API só leem a sessão.
"""

import logging
import time

import httpx

from app.cache import cached
from app.meuerp_client import MeuERPClient
from app.sqlquery import executar

logger = logging.getLogger(__name__)

# Permissões do Meu ERP que liberam cada parte do app (os ids são os mesmos em todas as empresas). Uma área com
# mais de um id libera com qualquer um deles.
PERMISSOES = {
    "produtos": (1,),               # cadastro de mercadoria: acessar o módulo
    "custo": (9,),                  # visualizar custos no cadastro de mercadoria
    "estoque": (10041, 1901),       # POS: consultar estoque | movimento de estoque
    "historico_preco": (5201, 3501),  # formação de preços | tabelas de preço
    "compras": (6401, 6501),        # central de compras | outras entradas (notas de entrada)
    "dashboard": (3301,),           # dashboard: acessar o módulo
    "lucro": (3304,),               # dashboard 'Resumo - Lucro' (margem e lucro bruto)
    "estoque_painel": (3320,),      # seção de dashboard 'Estoque'
    "conferencia": (4001,),         # conferência de caixa
    "financeiro": (2401,),          # contas a pagar/receber: acessar o módulo
    "pagar": (2402,),
    "receber": (2403,),
}
ABAS = ("dashboard", "produtos", "compras", "conferencia", "financeiro")
VERSAO = 1   # sessões sem os acessos desta versão entram de novo

ERRO_SEM_AREA = "Seu perfil no Meu ERP não libera nenhuma área do Prime Info. Fale com o responsável da loja."
ERRO_SEM_PERMISSAO = "Seu perfil no Meu ERP não tem acesso a esta área."

TTL_PERMISSOES = 3600
TTL_USUARIO = 6 * 3600            # perfil de um usuário já encontrado: confere de novo depois disso
REVER_IDS_A_CADA = 10 * 60        # usuário novo no ERP: a lista de quem tem perfil é relida no máximo a cada 10 min

_SQL_USUARIOS_COM_PERFIL = """
    select _idusuario as id, max(idperfil) as perfil from usuario_perfil_empresa
    where coalesce(idperfil, 0) > 0 and _idempresa = (select min(_idempresa) from mercadoria_variacao_empresa)
    group by 1 order by 1"""

# Por empresa: ids com perfil (e quando foram lidos) e o que já se sabe de cada id (e-mail, perfil, quando).
_usuarios: dict[str, dict] = {}


def calcular(liberadas: set[int]) -> dict:
    a = {area: any(p in liberadas for p in ids) for area, ids in PERMISSOES.items()}
    a["pagar"] = a["financeiro"] and a["pagar"]
    a["receber"] = a["financeiro"] and a["receber"]
    a["financeiro"] = a["pagar"] or a["receber"]
    a["lucro"] = a["dashboard"] and a["lucro"]
    a["estoque_painel"] = a["dashboard"] and a["estoque_painel"]
    return a


TUDO = {area: True for area in PERMISSOES}


async def _liberadas(tenant: dict, id_perfil: int) -> set[int]:
    async def ler() -> list[int]:
        client = MeuERPClient(tenant["api_token"])
        ids, pagina = [], 1
        while pagina <= 10:
            r = await client.get(f"/api/usuario-perfil/{int(id_perfil)}/permissoes/v1", params={"page": pagina, "limit": 100})
            ids += [int(i["idPermissao"]) for i in (r.get("items") or []) if i.get("tipoLiberacao") == "L"]
            if not r.get("hasNext"):
                break
            pagina += 1
        return ids
    return set(await cached((tenant["cnpj"], "info-permissoes-perfil", int(id_perfil)), TTL_PERMISSOES, ler,
                            obsoleto_ate=24 * 3600))


async def _perfil_do_email(tenant: dict, email: str) -> int | None:
    """Perfil do usuário deste e-mail na empresa (None se ele não tem perfil nela)."""
    cnpj = tenant["cnpj"]
    agora = time.time()
    dados = _usuarios.setdefault(cnpj, {"ids": [], "perfil_banco": {}, "ids_em": 0.0, "por_id": {}})
    client = MeuERPClient(tenant["api_token"])

    async def ler_usuario(uid: int) -> None:
        try:
            u = await client.get(f"/api/usuario/v1/{uid}")
            # A API às vezes não traz o perfil: vale o do banco do ERP (mesma empresa).
            perfil = int(u.get("idPerfil") or dados["perfil_banco"].get(uid) or 0)
            dados["por_id"][uid] = {"email": (u.get("email") or "").strip().lower(), "perfil": perfil, "em": agora}
        except httpx.HTTPStatusError:
            dados["por_id"][uid] = {"email": "", "perfil": 0, "em": agora}   # não ligado a esta empresa

    conhecido = next((uid for uid, u in dados["por_id"].items() if u["email"] == email), None)
    if conhecido is not None:
        if agora - dados["por_id"][conhecido]["em"] > TTL_USUARIO:
            await ler_usuario(conhecido)   # o perfil pode ter mudado
        perfil = dados["por_id"][conhecido]["perfil"]
        if perfil > 0 and dados["por_id"][conhecido]["email"] == email:
            return perfil
    if agora - dados["ids_em"] > REVER_IDS_A_CADA or not dados["ids"]:
        linhas = await executar(client, _SQL_USUARIOS_COM_PERFIL, max_paginas=20)
        dados["ids"] = [int(r["id"]) for r in linhas]
        dados["perfil_banco"] = {int(r["id"]): int(r["perfil"] or 0) for r in linhas}
        dados["ids_em"] = agora
    for uid in dados["ids"]:
        if uid in dados["por_id"] and agora - dados["por_id"][uid]["em"] <= TTL_USUARIO:
            continue
        await ler_usuario(uid)
        if dados["por_id"][uid]["email"] == email:
            break
    achado = next((u for u in dados["por_id"].values() if u["email"] == email and u["perfil"] > 0), None)
    return achado["perfil"] if achado else None


async def no_login(tenant: dict, email: str) -> tuple[dict | None, str | None]:
    """Depois da senha conferida: (dados para a sessão, None) ou (None, motivo da recusa)."""
    email = email.strip().lower()
    perfil = await _perfil_do_email(tenant, email)
    if perfil is None:   # sem perfil na empresa: vê tudo (decisão de 30/10/2026)
        logger.info("usuário sem perfil na empresa %s: acesso completo ao Prime Info", tenant["cnpj"])
        return {"acessos": dict(TUDO), "v": VERSAO}, None
    acessos = calcular(await _liberadas(tenant, perfil))
    if not any(acessos[a] for a in ABAS):
        return None, ERRO_SEM_AREA
    return {"perfil": perfil, "acessos": acessos, "v": VERSAO}, None


def na_sessao(request) -> dict | None:
    s = request.session
    return s.get("acessos") if s.get("acesso_v") == VERSAO else None


def gravar_na_sessao(request, dados: dict) -> None:
    request.session["acessos"] = dados["acessos"]
    request.session["acesso_v"] = dados["v"]
    request.session["perfil"] = dados.get("perfil")


async def atualizar(request, tenant: dict) -> dict:
    """Recalcula pelas permissões atuais do perfil (página principal). Se o ERP não responder, fica o da sessão."""
    atuais = na_sessao(request) or {}
    if not request.session.get("perfil"):   # sem perfil: continua vendo tudo
        return atuais
    try:
        novos = calcular(await _liberadas(tenant, int(request.session["perfil"])))
    except Exception:
        logger.warning("permissões do perfil indisponíveis agora (empresa %s); valem as do login", tenant["cnpj"])
        return atuais
    request.session["acessos"] = novos
    return novos


def pode(request, *areas: str) -> bool:
    acessos = na_sessao(request) or {}
    return any(acessos.get(a) for a in areas)

