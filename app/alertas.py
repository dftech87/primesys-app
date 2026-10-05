"""Alertas para o dono: estoque, notas fiscais rejeitadas, contas a pagar e a receber."""

from datetime import date, timedelta

from app import rest
from app.meuerp_client import MeuERPClient
from app.sqlquery import d, executar, i

_ATIVO = "coalesce(ve.flaginativo, 'N') <> 'S'"


def _num(valor) -> float:
    return float(valor or 0)


async def estoque(client: MeuERPClient, loja: int) -> dict:
    """Produtos com saldo negativo e abaixo do estoque mínimo no local de estoque escolhido."""
    base = f"""from mercadoria_estoque e
               join mercadoria_variacao_empresa ve
                 on ve._idempresa = e._idempresa and ve._idmercadoriavariacao = e._idmercadoriavariacao
               where e._idlocalestoque = {i(loja)} and {_ATIVO}"""
    contagens = await executar(
        client,
        f"""select count(*) filter (where e.qtdsaldo < 0) as negativos,
                   count(*) filter (where ve.qtdestoqueminimo > 0 and e.qtdsaldo < ve.qtdestoqueminimo) as abaixo_minimo
            {base}""",
    )
    lista = await executar(
        client,
        f"""select coalesce(nullif(trim(v.descricao), ''), m.descricao) as descricao,
                   e.qtdsaldo as saldo, ve.qtdestoqueminimo as minimo, v.embalagem as unidade
            from mercadoria_estoque e
            join mercadoria_variacao_empresa ve
              on ve._idempresa = e._idempresa and ve._idmercadoriavariacao = e._idmercadoriavariacao
            join mercadoria_variacao v on v._idmercadoriavariacao = e._idmercadoriavariacao
            join mercadoria m on m._idmercadoria = v.idmercadoria
            where e._idlocalestoque = {i(loja)} and {_ATIVO}
              and ve.qtdestoqueminimo > 0 and e.qtdsaldo < ve.qtdestoqueminimo
            order by (ve.qtdestoqueminimo - e.qtdsaldo) desc limit 8""",
    )
    base_linha = contagens[0] if contagens else {}
    return {
        "negativos": int(base_linha.get("negativos") or 0),
        "abaixoMinimo": int(base_linha.get("abaixo_minimo") or 0),
        "itens": [
            {"descricao": l["descricao"], "saldo": _num(l["saldo"]), "minimo": _num(l["minimo"]), "unidade": l["unidade"]}
            for l in lista
        ],
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


def _agrupar_vencimentos(itens: list[dict], hoje: date, parcial: bool) -> dict:
    grupos = {"atrasadas": [0, 0.0, []], "hoje": [0, 0.0, []], "proximos7": [0, 0.0, []]}
    for item in itens:
        vencimento = (item.get("dtVencimento") or "")[:10]
        if not vencimento:
            continue
        chave = "atrasadas" if vencimento < hoje.isoformat() else "hoje" if vencimento == hoje.isoformat() else "proximos7"
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
        client, caminho, {"inicio": "2000-01-01", "fim": (hoje + timedelta(days=7)).isoformat()}
    )
    return _agrupar_vencimentos(itens, hoje, parcial)
