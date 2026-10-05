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


async def formas_pagamento(client: MeuERPClient, inicio: date, fim: date) -> list[dict]:
    """Soma por forma de pagamento, com o troco (lançado como valor negativo) abatido do dinheiro
    e rótulos iguais agrupados (o ERP grava 'Pix' e 'PIX', 'A Prazo' e 'A PRAZO' separados)."""
    linhas = await executar(
        client,
        f"""select translate(upper(trim(coalesce(p.descricao, ''))), 'ÁÀÂÃÉÊÍÓÔÕÚÇ', 'AAAAEEIOOOUC') as forma,
                   round(sum(p.valor), 2) as total
            from documento_pagamento p join documento d on d._iddocumento = p._iddocumento
            where {_VENDA} and {_janela(inicio, fim)} group by 1""",
    )
    somas: dict[str, float] = {}
    for linha in linhas:
        forma = linha["forma"] or "OUTROS"
        if forma.startswith("TROCO"):
            forma = "DINHEIRO"
        somas[forma] = somas.get(forma, 0.0) + _num(linha["total"])
    positivas = {f: v for f, v in somas.items() if v > 0}
    total = sum(positivas.values())
    resultado = [
        {
            "forma": _ROTULO_PAGAMENTO.get(f, f.title()),
            "valor": round(v, 2),
            "percentual": round(v / total * 100, 1) if total else 0.0,
        }
        for f, v in positivas.items()
    ]
    return sorted(resultado, key=lambda x: x["valor"], reverse=True)


async def top_produtos(client: MeuERPClient, inicio: date, fim: date, limite: int = 10) -> list[dict]:
    linhas = await executar(
        client,
        f"""select max(dm.descricao) as descricao, sum(dm.qtd) as qtd, round(sum(dm.valtotalliquido), 2) as valor
            from documento d join documento_mercadoria dm on dm._iddocumento = d._iddocumento
            where {_VENDA} and {_janela(inicio, fim)}
            group by dm.idmercadoriavariacao order by qtd desc, valor desc limit {i(limite)}""",
    )
    return [{"descricao": l["descricao"], "quantidade": _num(l["qtd"]), "valor": _num(l["valor"])} for l in linhas]


async def margem(client: MeuERPClient, inicio: date, fim: date) -> dict | None:
    """Margem bruta estimada do período: venda dos itens menos o custo gravado no momento da venda.

    Conferida contra o DRE do ERP (HIPER, set/2026): 29,4% a 30,6% aqui contra 31,0% no DRE; o DRE
    também deixa de fora parte das vendas a prazo. Itens sem custo (0) ficam fora da conta, e a
    `cobertura` diz quanto da venda tinha custo (uma NF-e sem custo derruba a cobertura).
    """
    linhas = await executar(
        client,
        f"""select sum(dm.valtotalliquido) as venda_itens,
                   sum(dm.valtotalliquido) filter (where c.valcusto > 0) as venda_com_custo,
                   sum(dm.qtd * c.valcusto) filter (where c.valcusto > 0) as custo
            from documento d
            join documento_mercadoria dm on dm._iddocumento = d._iddocumento
            left join documento_mercadoria_custo c
              on c._iddocumento = dm._iddocumento and c._idsequencia = dm._idsequencia
            where {_VENDA} and {_janela(inicio, fim)}""",
    )
    base = linhas[0] if linhas else {}
    venda_com_custo, custo, venda_itens = _num(base.get("venda_com_custo")), _num(base.get("custo")), _num(base.get("venda_itens"))
    if venda_com_custo <= 0:
        return None
    lucro = venda_com_custo - custo
    return {
        "lucro": round(lucro, 2),
        "margemPct": round(lucro / venda_com_custo * 100, 1),
        "coberturaPct": round(venda_com_custo / venda_itens * 100, 1) if venda_itens else 0.0,
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
