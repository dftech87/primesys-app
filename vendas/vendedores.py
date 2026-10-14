"""Quem é o vendedor: o usuário do ERP (e-mail e senha) ligado a um funcionário ativo.

O ERP não diz qual usuário acabou de validar o login, então montamos o mapa e-mail -> funcionário, em duas fontes:
  1. o usuário vinculado ao funcionário (1 chamada para a lista + 1 por funcionário vinculado);
  2. se o e-mail não estiver lá, o e-mail do CONTATO no cadastro do funcionário (1 consulta SQL). Cobre quem não
     vinculou o usuário e usuários que o ERP não mostra (ex.: usuário de suporte/revenda). E-mail de contato
     repetido em dois funcionários ativos não vale (ambíguo).
A senha continua sendo conferida pelo ERP; o mapa só diz QUAL funcionário é. Fica em memória por algumas horas,
por empresa. O e-mail nunca vai para o celular: só nome e primeiro nome.
"""

import logging
import time

from app.meuerp_client import MeuERPClient
from app.sqlquery import executar
from vendas import config
from vendas.memoria import esquecer, gravar, guardado, vence_em

logger = logging.getLogger(__name__)

TTL_MAPA = 24 * 3600          # renovado em segundo plano a cada ~6 h (renovar_se_preciso), então o login não espera
TTL_PERMISSOES = 24 * 3600
RENOVAR_A_CADA = 6 * 3600
# E-mail fora do mapa (ex.: vínculo feito agora no ERP): remonta o mapa, mas no máximo a cada 10 minutos.
REMONTAR_NO_MINIMO_A_CADA = 10 * 60
_ultima_remontagem: dict[str, float] = {}


async def _todas_as_paginas(client: MeuERPClient, caminho: str, maximo_paginas: int = 10) -> list[dict]:
    itens: list[dict] = []
    for pagina in range(1, maximo_paginas + 1):
        resposta = await client.get(caminho, params={"page": pagina, "limit": 100})
        itens += resposta.get("items") or []
        if not resposta.get("hasNext"):
            break
    return itens


_SQL_EMAIL_CONTATO = """
    select distinct f._idpessoa as id, lower(trim(c.email)) as email
    from pessoa_funcionario f
    join pessoa_contato c on c._idpessoa = f._idpessoa
    where coalesce(f.flaginativo, 'F') <> 'T' and coalesce(trim(c.email), '') <> ''"""


async def _montar_mapa(token: str) -> dict[str, dict]:
    client = MeuERPClient(token)
    mapa: dict[str, dict] = {}
    funcionarios = [f for f in await _todas_as_paginas(client, "/api/funcionario/v1") if f.get("ativo") == "S"]

    # Fonte 2 (menor prioridade): e-mail de contato do cadastro do funcionário.
    por_id = {int(f["codigoPessoa"]): f for f in funcionarios}
    try:
        contatos = await executar(client, _SQL_EMAIL_CONTATO, max_paginas=20)
    except Exception:
        logger.warning("e-mails de contato dos funcionários indisponíveis; só o vínculo de usuário vale agora")
        contatos = []
    donos: dict[str, set[int]] = {}
    for c in contatos:
        if int(c["id"]) in por_id:
            donos.setdefault(c["email"], set()).add(int(c["id"]))
    for email, ids in donos.items():
        if len(ids) != 1:
            logger.warning("e-mail de contato repetido em %d funcionários ativos: ignorado", len(ids))
            continue
        f = por_id[next(iter(ids))]
        mapa[email] = {"id_funcionario": int(f["codigoPessoa"]), "id_usuario": int(f.get("idUsuario") or 0),
                       "id_perfil": None, "per_comissao": float(f.get("perComissao") or 0), "nome_usuario": "",
                       "origem": "contato"}

    # Fonte 1 (vale mais): usuário do ERP vinculado ao funcionário.
    for f in funcionarios:
        id_usuario = f.get("idUsuario")
        if not id_usuario:
            continue
        try:
            usuario = await client.get(f"/api/usuario/v1/{id_usuario}")
        except Exception:
            logger.warning("usuário %s do funcionário %s não encontrado no ERP", id_usuario, f.get("codigoPessoa"))
            continue
        email = (usuario.get("email") or "").strip().lower()
        if email:
            mapa[email] = {
                "id_funcionario": int(f["codigoPessoa"]),
                "id_usuario": int(id_usuario),
                "id_perfil": usuario.get("idPerfil"),
                "per_comissao": float(f.get("perComissao") or 0),
                "nome_usuario": usuario.get("nome") or "",
                "origem": "usuario",
            }
    return mapa


async def do_email(tenant: dict, email: str) -> dict | None:
    """Funcionário ativo ligado ao usuário deste e-mail, com o nome do cadastro de pessoas. None se não houver."""
    cnpj, email = tenant["cnpj"], email.strip().lower()
    mapa = await _mapa(tenant)
    if email not in mapa and time.monotonic() - _ultima_remontagem.get(cnpj, -1e9) > REMONTAR_NO_MINIMO_A_CADA:
        _ultima_remontagem[cnpj] = time.monotonic()
        esquecer(cnpj, "mapa-vendedores")
        mapa = await _mapa(tenant)
    vendedor = mapa.get(email)
    if vendedor is None:
        return None
    nome = vendedor["nome_usuario"]
    try:   # nome do cadastro de pessoas (o do usuário às vezes é um apelido, ex.: "Suporte Fulano"); guardado 1 dia
        id_func = int(vendedor["id_funcionario"])
        pessoa = await guardado(cnpj, f"nome-funcionario-{id_func}", 24 * 3600,
                                lambda: _nome_pessoa(tenant["api_token"], id_func))
        nome = pessoa or nome
    except Exception:
        logger.warning("cadastro de pessoa do funcionário %s indisponível; usando o nome do usuário", vendedor["id_funcionario"])
    nome = " ".join(nome.split()).title()
    return {**vendedor, "nome": nome, "primeiro_nome": nome.split(" ")[0] if nome else ""}


async def _nome_pessoa(token: str, id_pessoa: int) -> str:
    return (await MeuERPClient(token).get(f"/api/pessoa/{id_pessoa}")).get("razaoSocial") or ""


async def _mapa(tenant: dict) -> dict:
    return await guardado(tenant["cnpj"], "mapa-vendedores", TTL_MAPA, lambda: _montar_mapa(tenant["api_token"]))


async def do_funcionario(tenant: dict, id_funcionario: int) -> dict | None:
    """Dados atuais do vendedor (comissão, perfil) pelo código do funcionário, para conferir cada pedido."""
    mapa = await _mapa(tenant)
    candidatos = [v for v in mapa.values() if v["id_funcionario"] == int(id_funcionario)]
    # Se o mesmo funcionário aparece pelas duas fontes, vale a do usuário vinculado (tem o perfil).
    candidatos.sort(key=lambda v: v.get("origem") != "usuario")
    return candidatos[0] if candidatos else None


async def renovar_se_preciso(tenant: dict) -> None:
    """Segundo plano: refaz o mapa de vendedores e as permissões dos perfis quando têm mais de ~6 h, antes de vencer.
    Assim um vínculo novo no ERP vale em poucas horas e nenhum login precisa refazer essas consultas."""
    cnpj = tenant["cnpj"]
    if vence_em(cnpj, "mapa-vendedores") > TTL_MAPA - RENOVAR_A_CADA:
        return
    mapa = await _montar_mapa(tenant["api_token"])
    gravar(cnpj, "mapa-vendedores", mapa, TTL_MAPA)
    for id_perfil in {v["id_perfil"] for v in mapa.values() if v.get("id_perfil")}:
        try:
            gravar(cnpj, f"permissoes-perfil-{int(id_perfil)}", await _ler_permissoes(tenant["api_token"], int(id_perfil)), TTL_PERMISSOES)
        except Exception:
            logger.warning("não foi possível renovar as permissões do perfil %s", id_perfil)


async def ainda_ativo(tenant: dict, id_usuario: int, id_funcionario: int) -> bool | None:
    """Reconferência periódica: o vendedor continua ativo e com o mesmo usuário? None se o ERP não respondeu."""
    try:
        mapa = await _mapa(tenant)
    except Exception:
        return None
    return any(v["id_usuario"] == id_usuario and v["id_funcionario"] == id_funcionario for v in mapa.values())


async def _ler_permissoes(token: str, id_perfil: int) -> dict:
    itens = await _todas_as_paginas(MeuERPClient(token), f"/api/usuario-perfil/{id_perfil}/permissoes/v1")
    liberadas = {int(i["idPermissao"]) for i in itens if i.get("tipoLiberacao") == "L"}
    return {
        "desconto_item": config.PERMISSAO_DESCONTO_ITEM in liberadas,
        "desconto_documento": config.PERMISSAO_DESCONTO_DOCUMENTO in liberadas,
        "desconto_acima_do_limite": config.PERMISSAO_DESCONTO_ACIMA_DO_LIMITE in liberadas,
        "acrescimo_documento": config.PERMISSAO_ACRESCIMO_DOCUMENTO in liberadas,
    }


SEM_PERMISSAO = {"desconto_item": False, "desconto_documento": False, "desconto_acima_do_limite": False, "acrescimo_documento": False}


async def permissoes(tenant: dict, id_perfil: int | None) -> dict:
    """O que o perfil do usuário pode fazer no app. Na dúvida (sem perfil, ERP fora), nada de desconto."""
    if not id_perfil:
        return dict(SEM_PERMISSAO)
    try:
        return await guardado(tenant["cnpj"], f"permissoes-perfil-{int(id_perfil)}", TTL_PERMISSOES,
                              lambda: _ler_permissoes(tenant["api_token"], int(id_perfil)))
    except Exception:
        logger.exception("permissões do perfil %s indisponíveis; desconto bloqueado por segurança", id_perfil)
        return dict(SEM_PERMISSAO)
