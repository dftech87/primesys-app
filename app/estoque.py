"""Posição do estoque para o dashboard: valor a custo e a preço de venda, e contagem de produtos.

É uma foto do dia (o dono acompanha por dia), então quem chama guarda o resultado por horas e não
consulta o ERP a cada abertura do app. Uma consulta só, somando todos os locais de estoque.

Regras: só produtos ATIVOS e só saldo POSITIVO entram nos valores (saldo negativo não é mercadoria na
prateleira; é contado à parte). Produto com saldo mas sem custo (ou sem preço) cadastrado fica fora do
valor correspondente e é contado, para o dono saber o quanto o valor está subestimado.
"""

import time

from app.meuerp_client import MeuERPClient
from app.sqlquery import executar

# O ERP grava 'T'/'F' em flaginativo (alguns cadastros antigos usam 'S'/'N').
_ATIVO = "coalesce(ve.flaginativo, 'F') not in ('T', 'S')"


def _num(valor) -> float:
    return float(valor or 0)


async def posicao(client: MeuERPClient) -> dict:
    linhas = await executar(
        client,
        f"""with preco as (
              select distinct on (t._idmercadoriavariacao) t._idmercadoriavariacao as idv, t.valpreco as preco
              from mercadoria_tabela_preco t
              where coalesce(t.flaginativo, 'F') <> 'T' and t.valpreco > 0
              order by t._idmercadoriavariacao, t._idtabela
            ), saldo as (
              select e._idempresa as emp, e._idmercadoriavariacao as idv, sum(e.qtdsaldo) as saldo
              from mercadoria_estoque e group by 1, 2
            )
            select count(distinct ve._idmercadoriavariacao) as ativas,
                   count(distinct ve._idmercadoriavariacao) filter (where s.saldo > 0) as com_estoque,
                   count(distinct ve._idmercadoriavariacao) filter (where s.saldo < 0) as negativos,
                   round(sum(s.saldo) filter (where s.saldo > 0), 0) as unidades,
                   round(sum(s.saldo * c.valcusto) filter (where s.saldo > 0 and c.valcusto > 0), 2) as valor_custo,
                   round(sum(s.saldo * p.preco) filter (where s.saldo > 0), 2) as valor_venda,
                   count(distinct ve._idmercadoriavariacao) filter (where s.saldo > 0 and coalesce(c.valcusto, 0) <= 0) as sem_custo,
                   count(distinct ve._idmercadoriavariacao) filter (where s.saldo > 0 and p.preco is null) as sem_preco
            from mercadoria_variacao_empresa ve
            left join saldo s on s.emp = ve._idempresa and s.idv = ve._idmercadoriavariacao
            left join mercadoria_custo c on c._idempresa = ve._idempresa and c._idmercadoriavariacao = ve._idmercadoriavariacao
            left join preco p on p.idv = ve._idmercadoriavariacao
            where {_ATIVO}""",
    )
    base = linhas[0] if linhas else {}
    return {
        "geradoEm": time.time(),
        "ativas": int(base.get("ativas") or 0),
        "comEstoque": int(base.get("com_estoque") or 0),
        "negativos": int(base.get("negativos") or 0),
        "unidades": _num(base.get("unidades")),
        "valorCusto": _num(base.get("valor_custo")),
        "valorVenda": _num(base.get("valor_venda")),
        "semCusto": int(base.get("sem_custo") or 0),
        "semPreco": int(base.get("sem_preco") or 0),
    }
