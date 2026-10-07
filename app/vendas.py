"""Consultas de vendas (SQL no banco do cliente), usadas pelo dashboard web e mobile.

O que conta como "venda" (validado contra o endpoint mercadorias-vendidas do ERP em
três lojas — o total bate centavo a centavo):
  - modelo 55 / 65 / PV  (PV = pré-venda, como muitos supermercados vendem)
  - tipomovimento = 'S'  (saída; modelo 55 com 'E' é COMPRA de mercadoria)
  - status = 'E'         (emitido; 'C' cancelado, 'R' rejeitado, 'X' teste/rascunho)
Filtrar só por modelo infla o total (notas de compra e testes entram como venda).
O valor de cada venda é getvaltotal(): igual à soma dos itens e dos pagamentos.
"""

from datetime import date, timedelta

from app.meuerp_client import MeuERPClient
from app.sqlquery import d, executar, i

MODELOS_VENDA = ("55", "65", "PV")
_MODELOS = ",".join(f"'{m}'" for m in MODELOS_VENDA)
_VENDA = f"d.modelo in ({_MODELOS}) and d.tipomovimento = 'S' and d.status = 'E'"
_CANCELADA = f"d.modelo in ({_MODELOS}) and d.tipomovimento = 'S' and d.status = 'C'"

_ROTULO_PAGAMENTO = {
    "DINHEIRO": "Dinheiro",
    "PIX": "Pix",
    "CARTAO DEBITO": "Cartão de débito",
    "CARTAO CREDITO": "Cartão de crédito",
    "A PRAZO": "A prazo (fiado)",
    "PIX POS": "Pix (POS)",
    "VALE REFEICAO": "Vale refeição",
    "VALE ALIMENTACAO": "Vale alimentação",
}


def _janela(inicio: date, fim: date) -> str:
    """Intervalo [inicio, fim] inclusivo nos dois lados, como limite superior exclusivo."""
    return f"d.datahora >= {d(inicio)} and d.datahora < {d(fim + timedelta(days=1))}"


def _num(valor) -> float:
    return float(valor or 0)


async def resumo(client: MeuERPClient, inicio: date, fim: date) -> dict:
    linhas = await executar(
        client,
        f"""select count(*) as vendas, coalesce(round(sum(getvaltotal(d._iddocumento)), 2), 0) as total
            from documento d where {_VENDA} and {_janela(inicio, fim)}""",
    )
    vendas = int(linhas[0]["vendas"]) if linhas else 0
    total = _num(linhas[0]["total"]) if linhas else 0.0
    return {"vendas": vendas, "total": total, "ticket_medio": total / vendas if vendas else 0.0}


async def resumo_comparado(
    client: MeuERPClient, inicio: date, fim: date, comp_inicio: date, comp_fim: date
) -> tuple[dict, dict]:
    """Resumo do período e do período de comparação numa única consulta (o ERP limita 20 chamadas/min)."""
    atual = f"d.datahora >= {d(inicio)} and d.datahora < {d(fim + timedelta(days=1))}"
    comparacao = f"d.datahora >= {d(comp_inicio)} and d.datahora < {d(comp_fim + timedelta(days=1))}"
    linhas = await executar(
        client,
        f"""select count(*) filter (where {atual}) as vendas,
                   coalesce(round(sum(getvaltotal(d._iddocumento)) filter (where {atual}), 2), 0) as total,
                   count(*) filter (where {comparacao}) as vendas_comp,
                   coalesce(round(sum(getvaltotal(d._iddocumento)) filter (where {comparacao}), 2), 0) as total_comp
            from documento d where {_VENDA} and (({atual}) or ({comparacao}))""",
    )
    base = linhas[0] if linhas else {}

    def montar(vendas, total) -> dict:
        vendas, total = int(vendas or 0), _num(total)
        return {"vendas": vendas, "total": total, "ticket_medio": total / vendas if vendas else 0.0}

    return montar(base.get("vendas"), base.get("total")), montar(base.get("vendas_comp"), base.get("total_comp"))


async def vendas_por_dia(client: MeuERPClient, inicio: date, fim: date) -> dict[str, float]:
    linhas = await executar(
        client,
        f"""select to_char(d.datahora, 'YYYY-MM-DD') as dia,
                   round(sum(getvaltotal(d._iddocumento)), 2) as total
            from documento d where {_VENDA} and {_janela(inicio, fim)}
            group by 1 order by 1""",
        max_paginas=6,
    )
    return {linha["dia"]: _num(linha["total"]) for linha in linhas}


async def vendas_por_hora(client: MeuERPClient, dia: date) -> list[dict]:
    linhas = await executar(
        client,
        f"""select extract(hour from d.datahora)::int as hora, count(*) as vendas,
                   round(sum(getvaltotal(d._iddocumento)), 2) as total
            from documento d where {_VENDA} and {_janela(dia, dia)}
            group by 1 order by 1""",
    )
    return [{"hora": int(l["hora"]), "vendas": int(l["vendas"]), "valor": _num(l["total"])} for l in linhas]


# Nomes diferentes que o ERP de cada cliente dá à mesma forma de pagamento.
_SINONIMO_PAGAMENTO = {"VENDA A PRAZO": "A PRAZO"}
_TIPO_VENDA = {"65": "NFC-e", "55": "NF-e", "PV": "Pré-venda"}
_ORDEM_TIPO = {"65": 0, "55": 1, "PV": 2}   # fiscais primeiro, pré-venda (não fiscal) por último


def _lista_formas(somas: dict[str, float]) -> list[dict]:
    positivas = {f: v for f, v in somas.items() if v > 0}
    total = sum(positivas.values())
    resultado = [
        {
            "forma": _ROTULO_PAGAMENTO.get(f, f.capitalize()),
            "valor": round(v, 2),
            "percentual": round(v / total * 100, 1) if total else 0.0,
        }
        for f, v in positivas.items()
    ]
    return sorted(resultado, key=lambda x: x["valor"], reverse=True)


async def pagamentos(client: MeuERPClient, inicio: date, fim: date) -> tuple[list[dict], list[dict]]:
    """Formas de pagamento do período, no total e por tipo de venda (NFC-e, NF-e, pré-venda), numa só consulta.

    O troco (lançado como valor negativo) é abatido do dinheiro e rótulos iguais são agrupados (o ERP grava
    'Pix' e 'PIX', 'A Prazo' e 'A PRAZO' separados). Por tipo só é devolvido quando há mais de um tipo
    vendendo no período: com um só, o bloco não acrescenta nada."""
    linhas = await executar(
        client,
        f"""select modelo, forma, grouping(forma) as agregado, count(distinct id) as vendas, round(sum(valor), 2) as total
            from (select d.modelo, d._iddocumento as id, p.valor,
                         translate(upper(trim(coalesce(p.descricao, ''))), 'ÁÀÂÃÉÊÍÓÔÕÚÇ', 'AAAAEEIOOOUC') as forma
                  from documento_pagamento p join documento d on d._iddocumento = p._iddocumento
                  where {_VENDA} and {_janela(inicio, fim)}) x
            group by grouping sets ((modelo, forma), (modelo))""",
    )
    somas: dict[str, float] = {}
    tipos: dict[str, dict] = {}
    for linha in linhas:
        tipo = tipos.setdefault(linha["modelo"], {"vendas": 0, "total": 0.0, "somas": {}})
        if int(linha["agregado"]):
            tipo["vendas"], tipo["total"] = int(linha["vendas"]), _num(linha["total"])
            continue
        forma = linha["forma"] or "OUTROS"
        if forma.startswith("TROCO"):
            forma = "DINHEIRO"
        forma = _SINONIMO_PAGAMENTO.get(forma, forma)
        valor = _num(linha["total"])
        somas[forma] = somas.get(forma, 0.0) + valor
        tipo["somas"][forma] = tipo["somas"].get(forma, 0.0) + valor

    por_tipo: list[dict] = []
    soma_tipos = sum(t["total"] for t in tipos.values() if t["total"] > 0)
    if len([t for t in tipos.values() if t["total"] > 0]) > 1:
        for modelo in sorted(tipos, key=lambda m: _ORDEM_TIPO.get(m, 9)):
            t = tipos[modelo]
            if t["total"] <= 0:
                continue
            por_tipo.append({
                "tipo": _TIPO_VENDA.get(modelo, modelo),
                "fiscal": modelo != "PV",
                "vendas": t["vendas"],
                "total": round(t["total"], 2),
                "percentual": round(t["total"] / soma_tipos * 100, 1),
                "formas": _lista_formas(t["somas"]),
            })
    return _lista_formas(somas), por_tipo


async def top_produtos(client: MeuERPClient, inicio: date, fim: date, limite: int = 10) -> list[dict]:
    linhas = await executar(
        client,
        f"""select max(dm.descricao) as descricao, sum(dm.qtd) as qtd, round(sum(dm.valtotalliquido), 2) as valor
            from documento d join documento_mercadoria dm on dm._iddocumento = d._iddocumento
            where {_VENDA} and {_janela(inicio, fim)}
            group by dm.idmercadoriavariacao order by qtd desc, valor desc limit {i(limite)}""",
    )
    return [{"descricao": l["descricao"], "quantidade": _num(l["qtd"]), "valor": _num(l["valor"])} for l in linhas]


# Produto só entra no ranking de "margem mais baixa" se vendeu o bastante para a margem significar algo.
_VENDA_MINIMA_RANKING = 100
_LISTA_MARGEM = 5
# Comparar com o período anterior dobra o trabalho da consulta: só para períodos de até ~3 meses.
MAX_DIAS_COMPARACAO_MARGEM = 92


async def margem(
    client: MeuERPClient, inicio: date, fim: date, comp_inicio: date | None = None, comp_fim: date | None = None
) -> dict | None:
    """Margem bruta estimada do período, por produto, numa única consulta.

    Venda dos itens menos o custo gravado no momento da venda. Conferida contra o DRE do ERP (HIPER,
    set/2026): 29,4% a 30,6% aqui contra 31,0% no DRE; o DRE também deixa de fora parte das vendas a
    prazo. Itens sem custo (0) ficam fora da conta, e `coberturaPct` diz quanto da venda tinha custo
    (uma NF-e sem custo derruba a cobertura).

    Devolve a margem média (com a do período anterior, se pedida), os produtos com margem mais baixa
    (incluindo negativa), os de maior lucro e os itens vendidos abaixo do custo linha a linha
    (um produto em promoção abaixo do custo aparece mesmo que, no total, ainda dê lucro).
    """
    atual = f"d.datahora >= {d(inicio)} and d.datahora < {d(fim + timedelta(days=1))}"
    tem_comp = comp_inicio is not None and comp_fim is not None
    comp = f"d.datahora >= {d(comp_inicio)} and d.datahora < {d(comp_fim + timedelta(days=1))}" if tem_comp else "false"
    linhas = await executar(
        client,
        f"""with p as (
              select dm.idmercadoriavariacao as idv, max(dm.descricao) as descricao,
                     sum(dm.valtotalliquido) filter (where {atual}) as venda_itens,
                     sum(dm.valtotalliquido) filter (where {atual} and c.valcusto > 0) as venda,
                     sum(dm.qtd * c.valcusto) filter (where {atual} and c.valcusto > 0) as custo,
                     sum(dm.valtotalliquido) filter (where {comp} and c.valcusto > 0) as venda_comp,
                     sum(dm.qtd * c.valcusto) filter (where {comp} and c.valcusto > 0) as custo_comp,
                     sum(dm.qtd * c.valcusto - dm.valtotalliquido) filter (
                       where {atual} and c.valcusto > 0 and dm.qtd > 0
                         and dm.valtotalliquido < dm.qtd * c.valcusto - 0.02) as perda
              from documento d
              join documento_mercadoria dm on dm._iddocumento = d._iddocumento
              left join documento_mercadoria_custo c
                on c._iddocumento = dm._iddocumento and c._idsequencia = dm._idsequencia
              where {_VENDA} and (({atual}) or ({comp}))
              group by dm.idmercadoriavariacao
            ), t as (
              select p.*, venda - custo as lucro,
                     (venda - custo) / nullif(venda, 0) * 100 as margem,
                     sum(venda_itens) over () as g_venda_itens, sum(venda) over () as g_venda,
                     sum(custo) over () as g_custo, sum(venda_comp) over () as g_venda_comp,
                     sum(custo_comp) over () as g_custo_comp, sum(perda) over () as g_perda,
                     count(perda) over () as g_produtos_perda
              from p
            ), r as (
              select t.*,
                     case when venda > 0 then row_number() over (order by lucro desc nulls last) end as rn_lucro,
                     case when venda >= {_VENDA_MINIMA_RANKING} then row_number() over (
                          partition by (venda >= {_VENDA_MINIMA_RANKING}) order by margem asc) end as rn_margem,
                     case when perda > 0 then row_number() over (order by perda desc nulls last) end as rn_perda
              from t
            )
            select idv, descricao, round(venda, 2) as venda, round(custo, 2) as custo, round(lucro, 2) as lucro,
                   round(margem, 1) as margem, round(perda, 2) as perda,
                   rn_lucro, rn_margem, rn_perda,
                   g_venda_itens, g_venda, g_custo, g_venda_comp, g_custo_comp, g_perda, g_produtos_perda
            from r
            where rn_lucro <= {_LISTA_MARGEM} or rn_margem <= {_LISTA_MARGEM} or rn_perda <= {_LISTA_MARGEM}""",
    )
    if not linhas:
        return None
    g = linhas[0]
    venda, custo, venda_itens = _num(g["g_venda"]), _num(g["g_custo"]), _num(g["g_venda_itens"])
    if venda <= 0:
        return None
    lucro = venda - custo

    venda_comp, custo_comp = _num(g["g_venda_comp"]), _num(g["g_custo_comp"])
    margem_comp = round((venda_comp - custo_comp) / venda_comp * 100, 1) if venda_comp > 0 else None

    def _ordenado(campo: str, limite: int = _LISTA_MARGEM) -> list[dict]:
        escolhidos = sorted((l for l in linhas if l[campo] is not None and l[campo] <= limite), key=lambda l: l[campo])
        return [
            {
                "codigo": l["idv"], "descricao": l["descricao"], "venda": _num(l["venda"]), "lucro": _num(l["lucro"]),
                "margemPct": _num(l["margem"]), "perda": _num(l["perda"]),
            }
            for l in escolhidos
        ]

    return {
        "lucro": round(lucro, 2),
        "margemPct": round(lucro / venda * 100, 1),
        "margemAnteriorPct": margem_comp,
        "coberturaPct": round(venda / venda_itens * 100, 1) if venda_itens else 0.0,
        "menorMargem": _ordenado("rn_margem"),
        "maiorLucro": _ordenado("rn_lucro"),
        "abaixoDoCusto": {
            "produtos": int(g["g_produtos_perda"] or 0),
            "perda": round(_num(g["g_perda"]), 2),
            "itens": _ordenado("rn_perda"),
        },
    }


async def cancelamentos(client: MeuERPClient, inicio: date, fim: date) -> dict:
    resumo_linhas = await executar(
        client,
        f"""select count(*) as quantidade, coalesce(round(sum(getvaltotal(d._iddocumento)), 2), 0) as valor
            from documento d where {_CANCELADA} and {_janela(inicio, fim)}""",
    )
    base = resumo_linhas[0] if resumo_linhas else {}
    motivos = []
    if int(base.get("quantidade") or 0):  # sem cancelamento, não gasta uma chamada à toa
        motivos = await executar(
            client,
            f"""select coalesce(nullif(trim(h.motivo), ''), 'Sem motivo informado') as motivo, count(*) as n
                from documento d join documento_cancelamento_historico h on h._iddocumento = d._iddocumento
                where {_CANCELADA} and {_janela(inicio, fim)} group by 1 order by 2 desc limit 3""",
        )
    return {
        "quantidade": int(base.get("quantidade") or 0),
        "valor": _num(base.get("valor")),
        "motivos": [{"motivo": m["motivo"], "quantidade": int(m["n"])} for m in motivos],
    }
