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

from app import desempenho
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
    "alterar_preco": (2, 5202),     # cadastro de mercadoria: alterar preço de venda | formação de preços: alterar preço
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
    a["alterar_preco"] = a["produtos"] and a["alterar_preco"]
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


TTL_EMAILS_DA_EMPRESA = 10 * 60


async def _emails_da_empresa(tenant: dict) -> set[str] | None:
    """E-mails de todos os usuários da empresa (1 chamada a cada 100 usuários; guardado 10 min). A listagem do ERP traz só e-mail
    (sem código nem perfil), mas já basta para saber se um e-mail PERTENCE à empresa. None se não deu para ler (quem chama
    faz a varredura de antes)."""
    async def ler() -> list[str]:
        client = MeuERPClient(tenant["api_token"])
        emails, pagina = [], 1
        while pagina <= 10:
            r = await client.get("/api/usuario/v1", params={"page": pagina, "limit": 100})
            emails += [str(u.get("email") or "").strip().lower() for u in (r.get("items") or [])]
            if not r.get("hasNext"):
                break
            pagina += 1
        return emails
    try:
        return set(await cached((tenant["cnpj"], "info-emails-empresa"), TTL_EMAILS_DA_EMPRESA, ler))
    except Exception:
        logger.warning("lista de usuários da empresa %s indisponível; o login vai procurar o usuário um por um", tenant["cnpj"])
        return None


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
            dados["por_id"][uid] = {"email": (u.get("email") or "").strip().lower(), "perfil": perfil, "em": agora,
                                    "nome": (u.get("nome") or "").strip()}
        except httpx.HTTPStatusError as erro:
            if erro.response.status_code == 429 or erro.response.status_code >= 500:
                # ERP sobrecarregado ou fora: não dá para afirmar que o usuário não tem perfil. Recusa o login
                # (a tela diz para tentar de novo) em vez de cair em "sem perfil = vê tudo".
                raise
            dados["por_id"][uid] = {"email": "", "perfil": 0, "em": agora}   # 4xx: não ligado a esta empresa

    conhecido = next((uid for uid, u in dados["por_id"].items() if u["email"] == email), None)
    if conhecido is not None:
        if agora - dados["por_id"][conhecido]["em"] > TTL_USUARIO:
            try:
                await ler_usuario(conhecido)   # o perfil pode ter mudado
            except httpx.HTTPError:
                logger.warning("perfil do usuário não reconferido agora (ERP indisponível); vale o guardado")
        perfil = dados["por_id"][conhecido]["perfil"]
        if perfil > 0 and dados["por_id"][conhecido]["email"] == email:
            return perfil
    if agora - dados["ids_em"] > REVER_IDS_A_CADA or not dados["ids"]:
        linhas = await executar(client, _SQL_USUARIOS_COM_PERFIL, max_paginas=20)
        dados["ids"] = [int(r["id"]) for r in linhas]
        dados["perfil_banco"] = {int(r["id"]): int(r["perfil"] or 0) for r in linhas}
        dados["ids_em"] = agora
    # Quem não é usuário da empresa (suporte, revenda) não tem perfil nela: não adianta ler os usuários um por um (numa empresa
    # com 14 perfis eram 14 chamadas à toa e o login chegava a minutos). A listagem só vale se tiver pelo menos tantos
    # usuários quanto os que têm perfil no banco; senão não é confiável e vale a varredura completa (nunca dar "vê tudo" por engano).
    if not any(u["email"] == email for u in dados["por_id"].values()):
        emails = await _emails_da_empresa(tenant)
        if emails is not None and email not in emails and len(emails) >= len(dados["ids"]):
            logger.info("e-mail fora da lista de usuários da empresa %s: sem perfil, sem varredura", cnpj)
            return None
    for uid in dados["ids"]:
        if uid in dados["por_id"] and agora - dados["por_id"][uid]["em"] <= TTL_USUARIO:
            continue
        await ler_usuario(uid)
        if dados["por_id"][uid]["email"] == email:
            break
    achado = next((u for u in dados["por_id"].values() if u["email"] == email and u["perfil"] > 0), None)
    return achado["perfil"] if achado else None


def nome_do_email(cnpj: str, email: str) -> str:
    """Nome do usuário no ERP, se já foi lido (não chama o ERP). Quem não tem perfil na empresa não é encontrado: ''."""
    email = (email or "").strip().lower()
    for u in _usuarios.get(cnpj, {}).get("por_id", {}).values():
        if u.get("email") == email and u.get("nome"):
            return u["nome"]
    return ""


async def no_login(tenant: dict, email: str) -> tuple[dict | None, str | None]:
    """Depois da senha conferida: (dados para a sessão, None) ou (None, motivo da recusa)."""
    email = email.strip().lower()
    with desempenho.passo("perfil"):
        perfil = await _perfil_do_email(tenant, email)
    if perfil is None:   # sem perfil na empresa: vê tudo (decisão de 30/10/2026)
        logger.info("usuário sem perfil na empresa %s: acesso completo ao Prime Info", tenant["cnpj"])
        return {"acessos": dict(TUDO), "v": VERSAO}, None
    with desempenho.passo("permissoes"):
        liberadas = await _liberadas(tenant, perfil)
    acessos = calcular(liberadas)
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
    if not request.session.get("perfil"):   # sem perfil: continua vendo tudo (inclui áreas criadas depois do login)
        request.session["acessos"] = dict(TUDO)
        return dict(TUDO)
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


def pode_alterar_preco(request) -> bool:
    """Alterar preço escreve no ERP: além da permissão, a sessão precisa saber o e-mail (é quem fica no registro).
    Sessões abertas antes disso não o têm e voltam a ter o lápis no próximo login."""
    return bool(pode(request, "alterar_preco") and pode(request, "produtos") and request.session.get("email"))

