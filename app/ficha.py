"""Ficha do produto e estado do preço em UMA consulta SQL cada, em vez de 3 a 4 chamadas REST.

O ERP deixa só 20 chamadas por minuto por cliente (12 para o Prime Info): a ficha por REST gastava 4 (cadastro, preço,
custo, saldo) e abrir a folha de alterar preço mais 2. Aqui cada uma é uma consulta só. As regras são as mesmas do caminho
antigo, que continua como reserva se a consulta falhar: preço = tabela ativa de menor código; empresa = a primeira do cadastro.
"""

from app.meuerp_client import MeuERPClient
from app.sqlquery import executar, i

_EMPRESA = "(select min(_idempresa) from mercadoria_variacao_empresa)"
# O ERP grava 'T'/'F' em flaginativo (alguns cadastros antigos usam 'S'/'N').
_PRECO_ATIVO = "coalesce(p.flaginativo, 'F') not in ('T', 'S')"


def _numero(valor) -> float | None:
    return None if valor is None else float(valor)


async def ler(client: MeuERPClient, id_variacao: int, loja: int) -> dict | None:
    """Tudo da ficha numa chamada. None se o produto não existe (aí vale o caminho antigo, que responde 'não encontrado')."""
    linhas = await executar(
        client,
        f"""select coalesce(nullif(trim(v.descricao), ''), m.descricao) as descricao, v.codigobarras, v.embalagem,
                   coalesce(ve.flaginativo, 'F') as inativo,
                   (select p.valpreco from mercadoria_tabela_preco p
                     where p._idmercadoriavariacao = v._idmercadoriavariacao and p._idempresa = {_EMPRESA} and {_PRECO_ATIVO}
                     order by p._idtabela limit 1) as preco,
                   (select c.valcusto from mercadoria_custo c
                     where c._idmercadoriavariacao = v._idmercadoriavariacao and c._idempresa = {_EMPRESA}) as custo,
                   (select e.qtdsaldo from mercadoria_estoque e
                     where e._idmercadoriavariacao = v._idmercadoriavariacao and e._idempresa = {_EMPRESA}
                       and e._idlocalestoque = {i(loja)}) as saldo
            from mercadoria_variacao v
            join mercadoria m on m._idmercadoria = v.idmercadoria
            left join mercadoria_variacao_empresa ve on ve._idmercadoriavariacao = v._idmercadoriavariacao and ve._idempresa = {_EMPRESA}
            where v._idmercadoriavariacao = {i(id_variacao)}""",
        max_paginas=1,
    )
    if not linhas:
        return None
    l = linhas[0]
    return {
        "descricao": l.get("descricao"),
        "codigoBarras": l.get("codigobarras"),
        "unidade": l.get("embalagem"),
        "ativo": l.get("inativo") not in ("T", "S"),
        "precoVenda": _numero(l.get("preco")),
        "precoCusto": _numero(l.get("custo")),
        "estoque": _numero(l.get("saldo")),
    }


async def estado_do_preco(client: MeuERPClient, id_variacao: int) -> tuple[list[dict], float | None]:
    """Tabelas de preço ativas (a primeira é a que a ficha mostra) e o custo, lidos agora, numa chamada só."""
    linhas = await executar(
        client,
        f"""select p._idtabela as tabela, p.valpreco as preco, coalesce(p.flagvalidarvigencia, 'F') as vigencia,
                   (select c.valcusto from mercadoria_custo c
                     where c._idmercadoriavariacao = p._idmercadoriavariacao and c._idempresa = {_EMPRESA}) as custo
            from mercadoria_tabela_preco p
            where p._idmercadoriavariacao = {i(id_variacao)} and p._idempresa = {_EMPRESA} and {_PRECO_ATIVO}
            order by p._idtabela""",
        max_paginas=1,
    )
    tabelas = [{"codigo": int(l["tabela"]), "preco": float(l.get("preco") or 0), "vigencia": l.get("vigencia") in ("T", "S")} for l in linhas]
    return tabelas, (_numero(linhas[0].get("custo")) if linhas else None)
