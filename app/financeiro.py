"""Contas a pagar e a receber em aberto, para a aba Financeiro: totais por faixa de vencimento e os títulos
mais antigos de cada faixa.

Uma consulta só, com totais EXATOS (não depende de paginar a lista de títulos). Entram só parcelas em
aberto de documentos emitidos (status 'E'): notas rejeitadas ou de teste deixam parcelas "fantasma" no
banco que o ERP não mostra. Saldo = parcela menos o que já foi pago (pagamentos parciais).
"""

import time
from datetime import date, timedelta

from app.meuerp_client import MeuERPClient
from app.sqlquery import d, executar

TITULOS_POR_FAIXA = 8
_FAIXAS = [("atrasadas", "Atrasadas"), ("hoje", "Hoje"), ("prox7", "Próximos 7 dias"),
           ("prox30", "8 a 30 dias"), ("depois", "Depois de 30 dias")]
_NOME_GENERICO = {"", "CONSUMIDOR FINAL"}


def _num(valor) -> float:
    return float(valor or 0)


def _rotulo_titulo(nome: str, descricao: str) -> str:
    """Em alguns lançamentos o nome é o 'consumidor final' e o favorecido de verdade está na descrição."""
    nome, descricao = (nome or "").replace(" ", " ").strip(), (descricao or "").replace(" ", " ").strip()
    if nome.upper() in _NOME_GENERICO:
        return descricao or "Sem descrição"
    return nome


async def posicao(client: MeuERPClient, hoje: date) -> dict:
    linhas = await executar(
        client,
        f"""with pg as (
              select _iddocumento, _idsequencia, _idparcela, sum(valpagamento) as pago
              from documento_parcela_pagamento group by 1, 2, 3
            ), t as (
              select d.tipomovimentofinanceiro as tf, p.dtvencimento::date as venc,
                     round(p.valparcela - coalesce(pg.pago, 0), 2) as saldo,
                     trim(coalesce(pe.nome, '')) as nome,
                     d.naturezaoperacao as descricao, coalesce(nullif(trim(p.numero::text), ''), d.numero::text) as numero,
                     case when p.dtvencimento::date < {d(hoje)} then 'atrasadas'
                          when p.dtvencimento::date = {d(hoje)} then 'hoje'
                          when p.dtvencimento::date <= {d(hoje + timedelta(days=7))} then 'prox7'
                          when p.dtvencimento::date <= {d(hoje + timedelta(days=30))} then 'prox30'
                          else 'depois' end as faixa
              from documento_parcela p
              join documento d on d._iddocumento = p._iddocumento
              left join pg on pg._iddocumento = p._iddocumento and pg._idsequencia = p._idsequencia and pg._idparcela = p._idparcela
              left join pessoa pe on pe._idpessoa = d.idpessoa
              where p.status = 'P' and d.status = 'E' and d.tipomovimentofinanceiro in ('P', 'R')
                and p.valparcela - coalesce(pg.pago, 0) > 0.005
            ), r as (
              select t.*, count(*) over w as qtd, round(sum(saldo) over w, 2) as valor,
                     row_number() over (w order by venc, saldo desc) as rn
              from t window w as (partition by tf, faixa)
            )
            select tf, faixa, qtd, valor, nome, descricao, numero, to_char(venc, 'YYYY-MM-DD') as venc, saldo
            from r where rn <= {TITULOS_POR_FAIXA} order by tf, faixa, rn""",
        max_paginas=3,
    )

    por_tipo = {"P": {}, "R": {}}
    for linha in linhas:
        faixa = por_tipo[linha["tf"]].setdefault(
            linha["faixa"], {"quantidade": int(linha["qtd"]), "valor": _num(linha["valor"]), "titulos": []}
        )
        faixa["titulos"].append({
            "nome": _rotulo_titulo(linha["nome"], linha["descricao"]),
            "numero": linha["numero"],
            "vencimento": linha["venc"],
            "valor": _num(linha["saldo"]),
        })

    def montar(dados: dict) -> dict:
        faixas = []
        for chave, rotulo in _FAIXAS:
            f = dados.get(chave, {"quantidade": 0, "valor": 0.0, "titulos": []})
            if chave == "depois" and not f["quantidade"]:
                continue   # só aparece quando existe
            faixas.append({"id": chave, "rotulo": rotulo, **f})
        return {
            "quantidade": sum(f["quantidade"] for f in faixas),
            "valor": round(sum(f["valor"] for f in faixas), 2),
            "faixas": faixas,
        }

    return {"geradoEm": time.time(), "pagar": montar(por_tipo["P"]), "receber": montar(por_tipo["R"])}
