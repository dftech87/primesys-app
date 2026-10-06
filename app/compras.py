"""Entradas (compras) de um produto: as últimas notas fiscais em que ele chegou na loja."""

from app.meuerp_client import MeuERPClient
from app.sqlquery import executar, i

# NF-e de entrada (55) e nota modelo 1A. Os modelos AS/AC do ERP são acertos internos de estoque, não compras.
_MODELOS_ENTRADA = "('55', '1A')"
MAX_ENTRADAS = 30


def _num(valor) -> float:
    return float(valor or 0)


async def entradas_produto(client: MeuERPClient, id_variacao: int, limite: int = 10) -> list[dict]:
    """Últimas notas de entrada do produto, da mais nova para a mais antiga (uma consulta).

    `custoUnit` é o custo por unidade de estoque (já dividido pelo fator da embalagem); `precoVenda` é o preço de venda que o ERP gravou na
    entrada (pode vir 0 quando o ERP não gravou: a tela usa o preço atual nesse caso).
    """
    limite = max(1, min(int(limite), MAX_ENTRADAS))
    linhas = await executar(
        client,
        f"""select to_char(coalesce(d.datahoramovimento, d.datahora), 'DD/MM/YYYY') as data,
                   d.numero, d.serie, trim(coalesce(p.nome, '')) as fornecedor,
                   dm.qtd, dm.embalagem, dm.qtdfator as fator, dm.qtdestoque as qtd_estoque,
                   dm.valunitarioliquido as valor_nf, dm.valdesconto as desconto,
                   c.valcusto as custo_unit, c.valcustomedio as custo_medio,
                   (select pr.valpreco from documento_mercadoria_preco pr
                     where pr._iddocumento = dm._iddocumento and pr._idsequencia = dm._idsequencia
                     order by pr._idtabela limit 1) as preco_venda
            from documento d
            join documento_mercadoria dm on dm._iddocumento = d._iddocumento
            left join documento_mercadoria_custo c
              on c._iddocumento = dm._iddocumento and c._idsequencia = dm._idsequencia
            left join pessoa p on p._idpessoa = d.idpessoa
            where d.tipomovimento = 'E' and d.status = 'E' and d.modelo in {_MODELOS_ENTRADA}
              and dm.idmercadoriavariacao = {i(id_variacao)}
            order by coalesce(d.datahoramovimento, d.datahora) desc, d.numero desc
            limit {i(limite)}""",
    )
    resultado = []
    for l in linhas:
        fator = _num(l["fator"]) or 1.0
        valor_nf = _num(l["valor_nf"])
        # Custo por UNIDADE de estoque: o ERP grava em valcusto (a nota vem com o preço da embalagem,
        # ex.: caixa com 8 a R$ 8,64 = R$ 1,08 a unidade). Sem custo gravado, divide o valor da nota pelo fator.
        custo_unit = _num(l["custo_unit"]) or valor_nf / fator
        resultado.append(
            {
                "data": l["data"],
                "numero": l["numero"],
                "serie": l["serie"],
                "fornecedor": l["fornecedor"] or "Fornecedor não informado",
                "qtd": _num(l["qtd"]),
                "unidade": l["embalagem"],
                "fator": fator,
                "qtdUnidades": _num(l["qtd_estoque"]) or _num(l["qtd"]) * fator,
                "valorNF": valor_nf,
                "custoUnit": round(custo_unit, 4),
                "desconto": _num(l["desconto"]),
                "custoMedio": _num(l["custo_medio"]),
                "precoVenda": _num(l["preco_venda"]),
            }
        )
    return resultado
