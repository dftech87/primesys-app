"""Compras x vendas fiscais (aba Compras do Prime Info).

O que conta (pedido do dono, validado no cliente 36596421000174 em 19/11/2026):
  - Vendas fiscais: NFC-e (65) e NF-e (55) de SAÍDA com status 'E' (emitido). Pré-venda (PV) NÃO entra: é o que a SEFAZ enxerga.
  - Compras: só NF-e de ENTRADA (55) com status 'E' — o que a SEFAZ enxerga —, pela DATA DE EMISSÃO da nota, nunca pela
    de entrada (NF-e emitida em setembro e lançada em novembro conta em setembro). A emissão está em
    documento_eletronico.datahora (bate com o ano/mês da chave em 63 de 63 notas; documento.datahora é a data de lançamento).
    Nota modelo 1A (papel, sem chave, lançada como DIVERSO/CONSUMIDOR FINAL) NÃO entra (decisão do dono, 19/11/2026).
    Acertos de estoque (AC, AS, AE), IM e SF também não.
  - Valor: getvaltotal() do documento (o mesmo do Dashboard). A emissão é sempre anterior ou igual à entrada, então
    `d.datahora >= início` também vale para as compras e deixa o banco usar o índice de data.
Só as notas JÁ LANÇADAS no ERP: o manifesto (notas destinadas ao CNPJ) não está no banco nem na API do Meu ERP.
"""

from datetime import date, timedelta

from app.meuerp_client import MeuERPClient
from app.sqlquery import d, empresa, executar

_VENDA_FISCAL = "d.modelo in ('55', '65') and d.tipomovimento = 'S' and d.status = 'E'"
_COMPRA = "d.modelo = '55' and d.tipomovimento = 'E' and d.status = 'E'"
_EMISSAO_COMPRA = "coalesce(e.datahora, d.datahora)"

# Regime (CRT da empresa no ERP): 1 Simples, 2 Simples com excesso de sublimite, 4 MEI; 3 Regime Normal.
_CRT_SIMPLES = {"1", "2", "4"}
# Faixas da cor do aviso (compras como % das vendas fiscais): até "amarelo" é verde; até "vermelho" é amarelo; acima, vermelho.
# Simples: 80% é o limite da LC 123/2006, art. 29, X (no ano-calendário). Regime Normal: não há percentual na lei (sugestão).
LIMITES = {"simples": {"amarelo": 70, "vermelho": 80}, "normal": {"amarelo": 80, "vermelho": 100}}
FORNECEDORES_NA_LISTA = 5


def _num(valor) -> float:
    return float(valor or 0)


def _fim_exclusivo(fim: date) -> str:
    return d(fim + timedelta(days=1))


def _filtro(inicio: date, fim: date) -> str:
    """Vendas fiscais e compras do intervalo [inicio, fim], cada uma pela sua data (venda: a do documento; compra: a emissão)."""
    ini, ate = d(inicio), _fim_exclusivo(fim)
    return (f"(({_VENDA_FISCAL}) and d.datahora >= {ini} and d.datahora < {ate})"
            f" or (({_COMPRA}) and d.datahora >= {ini} and {_EMISSAO_COMPRA} >= {ini} and {_EMISSAO_COMPRA} < {ate})")


def regime(crt) -> str:
    return "simples" if str(crt or "").strip() in _CRT_SIMPLES else "normal"


async def serie_do_ano(client: MeuERPClient, ano: int, cnpj: str | None) -> dict:
    """Vendas fiscais e compras de cada mês do ano, e o regime da empresa, numa só consulta."""
    inicio, fim = date(ano, 1, 1), date(ano, 12, 31)
    linhas = await executar(
        client,
        f"""select x.mes, x.tipo, count(*) as notas, round(sum(getvaltotal(x.id)), 2) as total,
                   (select max(crt::text) from empresa where _idempresa = {empresa(cnpj)}) as crt
            from (select d._iddocumento as id,
                         case when {_VENDA_FISCAL} then 'V' else 'C' end as tipo,
                         extract(month from case when {_VENDA_FISCAL} then d.datahora else {_EMISSAO_COMPRA} end)::int as mes
                  from documento d left join documento_eletronico e on e._iddocumento = d._iddocumento
                  where {_filtro(inicio, fim)}) x
            group by 1, 2 order by 1, 2""",
        max_paginas=2,
    )
    meses = {m: {"mes": m, "vendas": 0.0, "notasVenda": 0, "compras": 0.0, "notasCompra": 0} for m in range(1, 13)}
    crt = None
    for linha in linhas:
        crt = crt or linha.get("crt")
        mes = meses[int(linha["mes"])]
        if linha["tipo"] == "V":
            mes["vendas"], mes["notasVenda"] = _num(linha["total"]), int(linha["notas"])
        else:
            mes["compras"], mes["notasCompra"] = _num(linha["total"]), int(linha["notas"])
    if crt is None:   # ano sem nenhuma nota: o regime vem numa consulta à parte só se for preciso (raro)
        crt = await _crt(client, cnpj)
    return {"ano": ano, "regime": regime(crt), "meses": [meses[m] for m in range(1, 13)]}


async def _crt(client: MeuERPClient, cnpj: str | None):
    linhas = await executar(client, f"select max(crt::text) as crt from empresa where _idempresa = {empresa(cnpj)}", max_paginas=1)
    return linhas[0]["crt"] if linhas else None


async def periodo(client: MeuERPClient, inicio: date, fim: date) -> dict:
    """Totais de um período qualquer (quando não dá para somar os meses da série)."""
    linhas = await executar(
        client,
        f"""select case when {_VENDA_FISCAL} then 'V' else 'C' end as tipo, count(*) as notas,
                   round(sum(getvaltotal(d._iddocumento)), 2) as total
            from documento d left join documento_eletronico e on e._iddocumento = d._iddocumento
            where {_filtro(inicio, fim)}
            group by 1""",
        max_paginas=1,
    )
    r = {"vendas": 0.0, "notasVenda": 0, "compras": 0.0, "notasCompra": 0}
    for linha in linhas:
        if linha["tipo"] == "V":
            r["vendas"], r["notasVenda"] = _num(linha["total"]), int(linha["notas"])
        else:
            r["compras"], r["notasCompra"] = _num(linha["total"]), int(linha["notas"])
    return r


async def fornecedores(client: MeuERPClient, inicio: date, fim: date) -> list[dict]:
    """Compras do período por fornecedor (pela emissão), da maior para a menor."""
    ini, ate = d(inicio), _fim_exclusivo(fim)
    linhas = await executar(
        client,
        f"""select d.idpessoa as id, max(trim(coalesce(p.nome, ''))) as nome, count(*) as notas,
                   round(sum(getvaltotal(d._iddocumento)), 2) as total
            from documento d
            left join documento_eletronico e on e._iddocumento = d._iddocumento
            left join pessoa p on p._idpessoa = d.idpessoa
            where {_COMPRA} and d.datahora >= {ini} and {_EMISSAO_COMPRA} >= {ini} and {_EMISSAO_COMPRA} < {ate}
            group by 1 order by 4 desc""",
        max_paginas=3,
    )
    return [{"nome": l["nome"] or "Fornecedor sem nome", "notas": int(l["notas"]), "total": _num(l["total"])} for l in linhas]


def periodo_pela_serie(serie: dict, inicio: date, fim: date, hoje: date) -> dict | None:
    """Soma meses inteiros da série quando o período é feito de meses inteiros do mesmo ano (até hoje, no mês corrente).
    Assim 'Este mês' e 'Este ano' não gastam consulta a mais. Fora disso devolve None (consulta própria)."""
    if inicio.year != serie["ano"] or fim.year != serie["ano"] or inicio.day != 1:
        return None
    ultimo_dia = (date(fim.year + (fim.month == 12), fim.month % 12 + 1, 1) - timedelta(days=1))
    if fim != ultimo_dia and fim < hoje:
        return None
    meses = [m for m in serie["meses"] if inicio.month <= m["mes"] <= fim.month]
    return {
        "vendas": round(sum(m["vendas"] for m in meses), 2),
        "notasVenda": sum(m["notasVenda"] for m in meses),
        "compras": round(sum(m["compras"] for m in meses), 2),
        "notasCompra": sum(m["notasCompra"] for m in meses),
    }


def _pct(compras: float, vendas: float) -> float | None:
    return round(compras / vendas * 100, 1) if vendas > 0 else None


def montar(serie: dict, totais: dict, lista_fornecedores: list[dict], inicio: date, fim: date, hoje: date) -> dict:
    """Resposta da tela: período, acumulado do ano (até o mês do fim do período), mês a mês e fornecedores."""
    ate_mes = fim.month   # a série é sempre do ano do fim do período
    if serie["ano"] == hoje.year:
        ate_mes = min(ate_mes, hoje.month)   # meses que ainda não chegaram não aparecem
    meses = [m for m in serie["meses"] if m["mes"] <= ate_mes]
    # O acumulado começa no 1º mês com venda fiscal no ERP: antes disso a loja vendia em outro sistema e só as compras
    # (emitidas antes, lançadas depois) estão no ERP — somá-las sem as vendas distorce o percentual (cliente 36596421000174:
    # 33 compras de outubro sem venda nenhuma no ERP davam ~228% no ano).
    desde_mes = next((m["mes"] for m in meses if m["vendas"] > 0), None)
    no_ano = [m for m in meses if desde_mes is not None and m["mes"] >= desde_mes]
    vendas_ano = round(sum(m["vendas"] for m in no_ano), 2)
    compras_ano = round(sum(m["compras"] for m in no_ano), 2)
    topo = lista_fornecedores[:FORNECEDORES_NA_LISTA]
    resto = lista_fornecedores[FORNECEDORES_NA_LISTA:]
    return {
        "periodo": {"inicio": inicio.isoformat(), "fim": fim.isoformat()},
        "regime": serie["regime"],
        "limites": LIMITES[serie["regime"]],
        "vendas": totais["vendas"], "notasVenda": totais["notasVenda"],
        "compras": totais["compras"], "notasCompra": totais["notasCompra"],
        "pct": _pct(totais["compras"], totais["vendas"]),
        "ano": {"ano": serie["ano"], "desdeMes": desde_mes, "ateMes": ate_mes, "vendas": vendas_ano, "compras": compras_ano,
                "pct": _pct(compras_ano, vendas_ano)},
        "meses": meses,
        "fornecedores": topo,
        "outros": {"qtd": len(resto), "total": round(sum(f["total"] for f in resto), 2)} if resto else None,
    }


def validar_periodo(inicio: date, fim: date) -> str | None:
    if fim < inicio:
        return "A data final é anterior à inicial."
    if (fim - inicio).days > 366:
        return "Escolha um período de até 1 ano."
    return None

