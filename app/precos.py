"""Preço de venda de vários produtos de uma vez, para a lista da busca do Prime Info.

Uma única consulta SQL traz o preço de todos os produtos da lista (até 20), em vez de uma chamada REST por produto
(que estouraria a cota do ERP). Vale a mesma regra da ficha do produto: a tabela de preço ativa de menor código.
"""

from app.meuerp_client import MeuERPClient
from app.sqlquery import executar, i

_ATIVO = "coalesce(p.flaginativo, 'F') not in ('T', 'S')"


async def da_lista(client: MeuERPClient, ids: list[int]) -> dict[int, float]:
    """{id da variação: preço de venda}. Produto sem preço (ou preço zero) fica de fora."""
    if not ids:
        return {}
    linhas = await executar(
        client,
        f"""select distinct on (p._idmercadoriavariacao) p._idmercadoriavariacao as id, p.valpreco as preco
            from mercadoria_tabela_preco p
            where p._idmercadoriavariacao in ({', '.join(i(x) for x in ids)})
              and p._idempresa = (select min(_idempresa) from mercadoria_variacao_empresa)
              and {_ATIVO}
            order by p._idmercadoriavariacao, p._idtabela""",
        max_paginas=1,
    )
    return {int(l["id"]): float(l["preco"]) for l in linhas if l.get("preco") is not None and float(l["preco"]) > 0}
