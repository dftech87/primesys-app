"""Quem é o vendedor: o usuário do ERP (e-mail e senha) ligado a um funcionário ativo.

O ERP não diz qual usuário acabou de validar o login, então montamos o mapa e-mail -> funcionário a partir dos
funcionários ativos que têm usuário vinculado (1 chamada para a lista + 1 por funcionário vinculado). O mapa fica
em memória por algumas horas, por empresa. O e-mail nunca vai para o celular: só nome e primeiro nome.
"""

import logging
import time

from app.cache import cached, descartar
from app.meuerp_client import MeuERPClient
from vendas import config

logger = logging.getLogger(__name__)

TTL_MAPA = 6 * 3600
TTL_PERMISSOES = 6 * 3600
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


async def _montar_mapa(token: str) -> dict[str, dict]:
    client = MeuERPClient(token)
    mapa: dict[str, dict] = {}
    for f in await _todas_as_paginas(client, "/api/funcionario/v1"):
        id_usuario = f.get("idUsuario")
        if not id_usuario or f.get("ativo") != "S":
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
            }
    return mapa


async def do_email(tenant: dict, email: str) -> dict | None:
    """Funcionário ativo ligado ao usuário deste e-mail, com o nome do cadastro de pessoas. None se não houver."""
    cnpj, email = tenant["cnpj"], email.strip().lower()
    chave = (cnpj, "vendas-mapa-vendedores")
    mapa = await cached(chave, TTL_MAPA, lambda: _montar_mapa(tenant["api_token"]))
    if email not in mapa and time.monotonic() - _ultima_remontagem.get(cnpj, -1e9) > REMONTAR_NO_MINIMO_A_CADA:
        _ultima_remontagem[cnpj] = time.monotonic()
        descartar(chave)
        mapa = await cached(chave, TTL_MAPA, lambda: _montar_mapa(tenant["api_token"]))
    vendedor = mapa.get(email)
    if vendedor is None:
        return None
    nome = vendedor["nome_usuario"]
    try:   # nome do cadastro de pessoas (o do usuário às vezes é um apelido, ex.: "Suporte Fulano")
        pessoa = await MeuERPClient(tenant["api_token"]).get(f"/api/pessoa/{vendedor['id_funcionario']}")
        nome = pessoa.get("razaoSocial") or nome
    except Exception:
        logger.warning("cadastro de pessoa do funcionário %s indisponível; usando o nome do usuário", vendedor["id_funcionario"])
    nome = " ".join(nome.split()).title()
    return {**vendedor, "nome": nome, "primeiro_nome": nome.split(" ")[0] if nome else ""}


async def ainda_ativo(tenant: dict, id_usuario: int, id_funcionario: int) -> bool | None:
    """Reconferência periódica: o vendedor continua ativo e com o mesmo usuário? None se o ERP não respondeu."""
    try:
        mapa = await cached((tenant["cnpj"], "vendas-mapa-vendedores"), TTL_MAPA, lambda: _montar_mapa(tenant["api_token"]))
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
        return await cached((tenant["cnpj"], "vendas-permissoes", int(id_perfil)), TTL_PERMISSOES,
                            lambda: _ler_permissoes(tenant["api_token"], int(id_perfil)))
    except Exception:
        logger.exception("permissões do perfil %s indisponíveis; desconto bloqueado por segurança", id_perfil)
        return dict(SEM_PERMISSAO)
