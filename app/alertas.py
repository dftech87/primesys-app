"""Alertas para o dono: estoque, notas fiscais rejeitadas, contas a pagar e a receber."""

from datetime import date, timedelta
from statistics import median

from app import rest, vendas
from app.meuerp_client import MeuERPClient
from app.sqlquery import d, executar, i

_ATIVO = "coalesce(ve.flaginativo, 'N') <> 'S'"


def _num(valor) -> float:
    return float(valor or 0)


async def estoque(client: MeuERPClient, loja: int) -> dict:
    """Produtos com saldo negativo e abaixo do estoque mínimo no local de estoque escolhido (1 consulta)."""
    linhas = await executar(
        client,
        f"""select coalesce(nullif(trim(v.descricao), ''), m.descricao) as descricao,
                   e.qtdsaldo as saldo, ve.qtdestoqueminimo as minimo, v.embalagem as unidade,
                   count(*) filter (where e.qtdsaldo < 0) over () as negativos,
                   count(*) filter (where ve.qtdestoqueminimo > 0 and e.qtdsaldo < ve.qtdestoqueminimo) over () as abaixo_minimo
            from mercadoria_estoque e
            join mercadoria_variacao_empresa ve
              on ve._idempresa = e._idempresa and ve._idmercadoriavariacao = e._idmercadoriavariacao
            join mercadoria_variacao v on v._idmercadoriavariacao = e._idmercadoriavariacao
            join mercadoria m on m._idmercadoria = v.idmercadoria
            where e._idlocalestoque = {i(loja)} and {_ATIVO}
            order by case when ve.qtdestoqueminimo > 0 and e.qtdsaldo < ve.qtdestoqueminimo
                          then ve.qtdestoqueminimo - e.qtdsaldo end desc nulls last
            limit 8""",
    )
    base_linha = linhas[0] if linhas else {}
    itens = [
        {"descricao": l["descricao"], "saldo": _num(l["saldo"]), "minimo": _num(l["minimo"]), "unidade": l["unidade"]}
        for l in linhas
        if _num(l["minimo"]) > 0 and _num(l["saldo"]) < _num(l["minimo"])  # as demais linhas só trazem os totais
    ]
    return {
        "negativos": int(base_linha.get("negativos") or 0),
        "abaixoMinimo": int(base_linha.get("abaixo_minimo") or 0),
        "itens": itens,
    }


async def notas_rejeitadas(client: MeuERPClient, hoje: date, dias: int = 7) -> dict:
    """Notas fiscais de venda (NF-e/NFC-e) rejeitadas pela SEFAZ nos últimos `dias` dias."""
    linhas = await executar(
        client,
        f"""select d.numero, d.modelo, to_char(d.datahora, 'DD/MM HH24:MI') as quando,
                   round(getvaltotal(d._iddocumento), 2) as valor, count(*) over () as total
            from documento d
            where d.modelo in ('55', '65') and d.tipomovimento = 'S' and d.status = 'R'
              and d.datahora >= {d(hoje - timedelta(days=dias))}
            order by d.datahora desc limit 5""",
    )
    return {
        "dias": dias,
        "total": int(linhas[0]["total"]) if linhas else 0,
        "itens": [
            {"numero": l["numero"], "modelo": l["modelo"], "quando": l["quando"], "valor": _num(l["valor"])}
            for l in linhas
        ],
    }


TITULOS_POR_GRUPO = 10
HORIZONTE_DIAS = 30


def _faixa(vencimento: str, hoje: date) -> str:
    dias = (date.fromisoformat(vencimento) - hoje).days
    if dias < 0:
        return "atrasadas"
    if dias == 0:
        return "hoje"
    if dias <= 7:
        return "proximos7"
    return "dias8a15" if dias <= 15 else "dias16a30"


def _agrupar_vencimentos(itens: list[dict], hoje: date, parcial: bool) -> dict:
    grupos = {
        "atrasadas": [0, 0.0, []], "hoje": [0, 0.0, []], "proximos7": [0, 0.0, []],
        "dias8a15": [0, 0.0, []], "dias16a30": [0, 0.0, []],
    }
    for item in itens:
        vencimento = (item.get("dtVencimento") or "")[:10]
        if not vencimento:
            continue
        chave = _faixa(vencimento, hoje)
        saldo = _num(item.get("valSaldo"))
        grupos[chave][0] += 1
        grupos[chave][1] += saldo
        grupos[chave][2].append(
            {"nome": item.get("nome") or "", "numero": item.get("numero"), "vencimento": vencimento, "valor": saldo}
        )

    resultado = {}
    for chave, (quantidade, valor, titulos) in grupos.items():
        # Mais antigos primeiro (em atraso, é o que mais pede atenção); só os primeiros vão para o celular.
        titulos.sort(key=lambda t: (t["vencimento"], -t["valor"]))
        resultado[chave] = {"quantidade": quantidade, "valor": round(valor, 2), "titulos": titulos[:TITULOS_POR_GRUPO]}
    resultado["parcial"] = parcial
    return resultado


async def contas(client: MeuERPClient, tipo: str, hoje: date) -> dict:
    """Contas pendentes ('pagar' ou 'receber'): atrasadas, vencendo hoje e nos próximos 7 dias."""
    caminho = "/api/conta-pagar/pendentes/v1" if tipo == "pagar" else "/api/conta-receber/pendentes/v1"
    itens, parcial = await rest.todas_paginas(
        client, caminho, {"inicio": "2000-01-01", "fim": (hoje + timedelta(days=HORIZONTE_DIAS)).isoformat()}
    )
    return _agrupar_vencimentos(itens, hoje, parcial)


async def venda_tipica_dia(client: MeuERPClient, hoje: date, dias: int = 28) -> float:
    """Venda média por dia nos últimos `dias`, sem dias fora do padrão (ex.: uma nota de 40x a mediana)."""
    por_dia = await vendas.vendas_por_dia(client, hoje - timedelta(days=dias), hoje - timedelta(days=1))
    valores = list(por_dia.values())
    if not valores:
        return 0.0
    teto = median(valores) * 10
    normais = [v for v in valores if v <= teto] or valores
    return round(sum(normais) / len(normais), 2)


# Janela do "parou de vender": vendia em boa parte dos dias do mês e não vendeu nada nos últimos 3 dias.
_DIAS_RECENTES = 3
_JANELA_BASE = 30
_MIN_DIAS_COM_VENDA = 15


async def ruptura(client: MeuERPClient, loja: int, hoje: date) -> dict:
    """Produtos que vendiam com regularidade e pararam de vender (provável falta ou problema de exposição).

    Saldo <= 0 e parou de vender: provavelmente acabou (repor). Saldo positivo e parou: conferir
    gôndola e preço. Saldo negativo SEM parar de vender não é falta: é lançamento de entrada atrasado.
    """
    base_ini = hoje - timedelta(days=_JANELA_BASE)
    recente_ini = hoje - timedelta(days=_DIAS_RECENTES)
    dias_base = (recente_ini - base_ini).days
    linhas = await executar(
        client,
        f"""with base as (
              select dm.idmercadoriavariacao as idv, max(dm.descricao) as descricao,
                     count(distinct d.datahora::date) filter (where d.datahora < {d(recente_ini)}) as dias_com_venda,
                     sum(dm.valtotalliquido) filter (where d.datahora < {d(recente_ini)}) as valor_antes,
                     sum(dm.qtd) filter (where d.datahora >= {d(recente_ini)}) as qtd_recente,
                     max(d.datahora) as ultima
              from documento d join documento_mercadoria dm on dm._iddocumento = d._iddocumento
              where {vendas._VENDA} and d.datahora >= {d(base_ini)} and d.datahora < {d(hoje + timedelta(days=1))}
              group by dm.idmercadoriavariacao
            ), parados as (
              select b.descricao, e.qtdsaldo as saldo, b.valor_antes / {i(dias_base)} as valor_dia,
                     b.dias_com_venda, to_char(b.ultima, 'DD/MM') as ultima
              from base b
              join mercadoria_estoque e on e._idmercadoriavariacao = b.idv and e._idlocalestoque = {i(loja)}
              join mercadoria_variacao_empresa ve
                on ve._idempresa = e._idempresa and ve._idmercadoriavariacao = e._idmercadoriavariacao
              where {_ATIVO} and b.dias_com_venda >= {i(_MIN_DIAS_COM_VENDA)} and coalesce(b.qtd_recente, 0) = 0
            )
            select descricao, saldo, round(valor_dia, 2) as valor_dia, dias_com_venda, ultima,
                   count(*) over () as total,
                   count(*) filter (where saldo <= 0) over () as total_repor,
                   round(sum(valor_dia) over (), 2) as valor_dia_total
            from parados order by valor_dia desc limit 30""",
    )
    primeira = linhas[0] if linhas else {}
    itens = [
        {
            "descricao": l["descricao"], "saldo": _num(l["saldo"]), "valorDia": _num(l["valor_dia"]),
            "diasComVenda": int(l["dias_com_venda"]), "ultimaVenda": l["ultima"],
        }
        for l in linhas
    ]
    return {
        "total": int(primeira.get("total") or 0),
        "totalRepor": int(primeira.get("total_repor") or 0),
        "valorDia": _num(primeira.get("valor_dia_total")),
        "diasBase": dias_base,
        "repor": [it for it in itens if it["saldo"] <= 0][:5],
        "conferir": [it for it in itens if it["saldo"] > 0][:5],
    }
