"""Endpoints do painel mobile: dashboard de vendas (com filtro de datas) e alertas do dono."""

import asyncio
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import alertas, caixa, vendas
from app.cache import cached
from app.deps import current_tenant
from app.meuerp_client import MeuERPClient
from app.sqlquery import ConsultaIndisponivel

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_DIAS = 366
TTL_DASHBOARD = 60            # período que inclui hoje: ainda está vendendo
TTL_CAIXA = 120               # período que inclui hoje (ainda há caixas fechando)
TTL_CAIXA_ENCERRADO = 900
TTL_DASHBOARD_ENCERRADO = 900  # período já fechado: praticamente não muda
_DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _data(texto: str | None, padrao: date) -> date:
    try:
        return date.fromisoformat(texto) if texto else padrao
    except ValueError:
        return padrao


def _data_do_cliente(texto: str | None) -> date:
    """A data do aparelho vale (o servidor pode estar em outro fuso), mas só ±1 dia da do servidor."""
    servidor = date.today()
    informada = _data(texto, servidor)
    return informada if abs((informada - servidor).days) <= 1 else servidor


def _comparacao_margem(inicio: date, fim: date, comp_ini: date, comp_fim: date):
    """A margem só é comparada com o período anterior em períodos curtos (dobra o trabalho da consulta)."""
    if (fim - inicio).days + 1 > vendas.MAX_DIAS_COMPARACAO_MARGEM:
        return None, None
    return comp_ini, comp_fim


def _periodo_comparacao(inicio: date, fim: date) -> tuple[date, date, str]:
    """Um dia é comparado com o mesmo dia da semana anterior (lojas têm picos semanais, então
    comparar com 'ontem' engana); intervalos maiores, com o período imediatamente anterior."""
    dias = (fim - inicio).days + 1
    if dias == 1:
        anterior = inicio - timedelta(days=7)
        return anterior, anterior, f"vs {_DIAS_SEMANA[anterior.weekday()]} anterior ({anterior:%d/%m})"
    comp_fim = inicio - timedelta(days=1)
    comp_ini = comp_fim - timedelta(days=dias - 1)
    return comp_ini, comp_fim, f"vs período anterior ({comp_ini:%d/%m}–{comp_fim:%d/%m})"


async def _opcional(nome: str, coro):
    """Informação extra do dashboard: se falhar, some da tela, mas não derruba o resto."""
    try:
        return await coro
    except Exception:
        logger.exception("'%s' indisponível no dashboard", nome)
        return None


async def _montar_dashboard(client: MeuERPClient, inicio: date, fim: date) -> dict:
    comp_ini, comp_fim, rotulo = _periodo_comparacao(inicio, fim)
    # O ERP deixa só 20 chamadas por minuto por cliente: cada consulta a menos deixa a tela mais rápida.
    # Um dia só dispensa a série por dia (o gráfico é por hora e o total já vem do resumo).
    um_dia = inicio == fim
    tarefas = [
        vendas.resumo_comparado(client, inicio, fim, comp_ini, comp_fim),
        vendas.pagamentos(client, inicio, fim),
        vendas.cancelamentos(client, inicio, fim),
        _opcional("margem", vendas.margem(client, inicio, fim, *_comparacao_margem(inicio, fim, comp_ini, comp_fim))),
        vendas.vendas_por_hora(client, inicio) if um_dia else vendas.vendas_por_dia(client, inicio, fim),
    ]
    (atual, anterior), (formas, por_tipo), cancel, margem, hora_ou_dia = await asyncio.gather(*tarefas)
    por_hora = hora_ou_dia if um_dia else None
    por_dia = {inicio.isoformat(): atual["total"]} if um_dia else hora_ou_dia

    serie_dias = []
    cursor = inicio
    while cursor <= fim:
        serie_dias.append({"dia": cursor.strftime("%d/%m"), "valor": por_dia.get(cursor.isoformat(), 0)})
        cursor += timedelta(days=1)

    soma_formas = sum(f["valor"] for f in formas)
    sem_pagamento = round(atual["total"] - soma_formas, 2)
    return {
        "periodo": {"inicio": inicio.isoformat(), "fim": fim.isoformat()},
        "total": atual["total"],
        "vendas": atual["vendas"],
        "ticketMedio": atual["ticket_medio"],
        "comparativo": {"rotulo": rotulo, "total": anterior["total"], "vendas": anterior["vendas"]},
        "vendasPorDia": serie_dias,
        "vendasPorHora": por_hora,
        "formasPagamento": formas,
        "vendasPorTipo": por_tipo,
        "semFormaPagamento": sem_pagamento if sem_pagamento > 1 else 0,
        "cancelamentos": cancel,
        "margem": margem,
    }


@router.get("/api/produtos/dashboard-mobile")
async def dashboard_mobile(request: Request, inicio: str | None = Query(None), fim: str | None = Query(None)):
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    hoje = date.today()
    data_inicio, data_fim = _data(inicio, hoje), _data(fim, hoje)
    if data_fim < data_inicio:
        data_inicio, data_fim = data_fim, data_inicio
    if (data_fim - data_inicio).days >= MAX_DIAS:
        data_inicio = data_fim - timedelta(days=MAX_DIAS - 1)

    client = MeuERPClient(tenant["api_token"])
    try:
        return await cached(
            (tenant["cnpj"], "dashboard", data_inicio, data_fim),
            TTL_DASHBOARD if data_fim >= hoje else TTL_DASHBOARD_ENCERRADO,
            lambda: _montar_dashboard(client, data_inicio, data_fim),
        )
    except ConsultaIndisponivel:
        logger.exception("consulta SQL indisponível (dashboard) para %s", tenant["cnpj"])
        return JSONResponse({"detail": "Consulta indisponível para esta empresa no momento."}, status_code=502)


async def _secao(nome: str, cnpj: str, coro):
    """Uma seção que falha não derruba as outras: vira None e a tela mostra 'indisponível'."""
    try:
        return await coro
    except Exception:
        logger.exception("alerta '%s' indisponível para %s", nome, cnpj)
        return None


# Validade de cada bloco dos alertas: o que muda devagar não precisa ser buscado a cada abertura da aba.
# (pagar/receber são o mais caro: 2 a 5 páginas cada)
TTL_ESTOQUE = 300
TTL_FISCAL = 300
TTL_CONTAS = 600
TTL_RUPTURA = 600
TTL_VENDA_TIPICA = 3600


def _bloco(cnpj: str, nome: str, ttl: float, chave: tuple, produzir):
    """Bloco dos alertas com cache próprio; se falhar, vira None (a tela mostra 'indisponível')."""
    return _secao(nome, cnpj, cached((cnpj, "alerta", nome) + chave, ttl, produzir, obsoleto_ate=ttl))


async def _montar_alertas(client: MeuERPClient, cnpj: str, loja: int, hoje: date) -> dict:
    estoque, fiscal, pagar, receber, ruptura, venda_dia = await asyncio.gather(
        _bloco(cnpj, "estoque", TTL_ESTOQUE, (loja,), lambda: alertas.estoque(client, loja)),
        _bloco(cnpj, "fiscal", TTL_FISCAL, (hoje,), lambda: alertas.notas_rejeitadas(client, hoje)),
        _bloco(cnpj, "pagar", TTL_CONTAS, (hoje,), lambda: alertas.contas(client, "pagar", hoje)),
        _bloco(cnpj, "receber", TTL_CONTAS, (hoje,), lambda: alertas.contas(client, "receber", hoje)),
        _bloco(cnpj, "ruptura", TTL_RUPTURA, (loja, hoje), lambda: alertas.ruptura(client, loja, hoje)),
        _bloco(cnpj, "venda_tipica", TTL_VENDA_TIPICA, (hoje,), lambda: alertas.venda_tipica_dia(client, hoje)),
    )
    return {
        "estoque": estoque, "notasRejeitadas": fiscal, "contasPagar": pagar, "contasReceber": receber,
        "ruptura": ruptura, "vendaTipicaDia": venda_dia,
    }


@router.get("/api/produtos/alertas")
async def alertas_mobile(
    request: Request,
    loja: int = Query(...),
    hoje: str | None = Query(None),
    segundo_plano: int = Query(0),
):
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    data_hoje = _data_do_cliente(hoje)
    # segundo_plano=1 (selo da aba, carregado sem ninguém esperando) usa só a cota que sobra do ERP
    client = MeuERPClient(tenant["api_token"], segundo_plano=bool(segundo_plano))
    return await _montar_alertas(client, tenant["cnpj"], loja, data_hoje)


@router.get("/api/produtos/fechamento-caixa")
async def fechamento_caixa(request: Request, inicio: str | None = Query(None), fim: str | None = Query(None)):
    """Fechamento de caixa do período: esperado, contado e diferença (contado - esperado) por forma de pagamento."""
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    hoje = date.today()
    data_inicio, data_fim = _data(inicio, hoje), _data(fim, hoje)
    if data_fim < data_inicio:
        data_inicio, data_fim = data_fim, data_inicio
    if (data_fim - data_inicio).days >= caixa.MAX_DIAS:
        data_inicio = data_fim - timedelta(days=caixa.MAX_DIAS - 1)

    client = MeuERPClient(tenant["api_token"])
    try:
        return await cached(
            (tenant["cnpj"], "caixa", data_inicio, data_fim),
            TTL_CAIXA if data_fim >= hoje else TTL_CAIXA_ENCERRADO,
            lambda: caixa.fechamentos(client, data_inicio, data_fim),
        )
    except ConsultaIndisponivel:
        logger.exception("consulta SQL indisponível (caixa) para %s", tenant["cnpj"])
        return JSONResponse({"detail": "Consulta indisponível para esta empresa no momento."}, status_code=502)
