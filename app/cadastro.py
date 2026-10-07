"""Produtos com margem negativa no CADASTRO: o preço de venda de hoje está abaixo do custo de hoje.

É o mesmo cálculo da tela de cadastro do ERP: margem = (preço de venda - custo) / preço de venda. O preço vem
da tabela de preço principal do produto (a de menor número que não esteja inativa) e o custo, do custo atual do
produto. Validado com o Limão (cód. 9263): preço 5,99 e custo 8,16 dão -36,2%, como no cadastro.

Não é a margem das vendas do período (que mistura os preços e custos de cada dia); é o que precisa de correção
no cadastro. Os produtos que venderam no último mês vêm primeiro: são os que mais custam dinheiro.
"""

from datetime import date, timedelta

from app.meuerp_client import MeuERPClient
from app.sqlquery import d, executar

DIAS_VENDA = 30
MAX_ITENS = 8


def _num(valor) -> float:
    return float(valor or 0)


async def margem_negativa(client: MeuERPClient, hoje: date) -> dict:
    linhas = await executar(
        client,
        f"""with preco as (
              select distinct on (t._idmercadoriavariacao) t._idmercadoriavariacao as idv, t.valpreco as preco
              from mercadoria_tabela_preco t
              where coalesce(t.flaginativo, 'F') <> 'T' and t.valpreco > 0
              order by t._idmercadoriavariacao, t._idtabela
            ), neg as (
              select p.idv, p.preco, c.valcusto as custo, (p.preco - c.valcusto) / p.preco * 100 as margem
              from preco p join mercadoria_custo c on c._idmercadoriavariacao = p.idv and c.valcusto > 0
              where p.preco < c.valcusto
            ), vend as (
              select dm.idmercadoriavariacao as idv, round(sum(dm.valtotalliquido), 2) as venda
              from documento d join documento_mercadoria dm on dm._iddocumento = d._iddocumento
              where d.datahora >= {d(hoje - timedelta(days=DIAS_VENDA))}
                and d.modelo in ('55', '65', 'PV') and d.tipomovimento = 'S' and d.status = 'E'
                and dm.idmercadoriavariacao in (select idv from neg)
              group by 1
            )
            select n.idv as codigo, coalesce(nullif(trim(v.descricao), ''), m.descricao) as produto,
                   n.preco, n.custo, round(n.margem, 2) as margem, x.venda
            from neg n
            join mercadoria_variacao v on v._idmercadoriavariacao = n.idv
            join mercadoria m on m._idmercadoria = v.idmercadoria
            join mercadoria_variacao_empresa ve on ve._idmercadoriavariacao = n.idv and coalesce(ve.flaginativo, 'N') <> 'S'
            left join vend x on x.idv = n.idv
            order by x.venda desc nulls last, n.margem asc""",
    )
    itens = [
        {
            "codigo": l["codigo"], "produto": l["produto"], "preco": _num(l["preco"]), "custo": _num(l["custo"]),
            "margemPct": _num(l["margem"]), "venda30d": _num(l["venda"]),
        }
        for l in linhas
    ]
    return {
        "total": len(itens),
        "vendendo": sum(1 for i in itens if i["venda30d"] > 0),
        "dias": DIAS_VENDA,
        "itens": itens[:MAX_ITENS],
    }
