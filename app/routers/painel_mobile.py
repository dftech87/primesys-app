"""Endpoints do painel mobile: dashboard de vendas (com filtro de datas) e alertas do dono."""

import asyncio
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import alertas, vendas
from app.cache import cached
from app.deps import current_tenant
from app.meuerp_client import MeuERPClient
from app.sqlquery import ConsultaIndisponivel

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_DIAS = 366
TTL_DASHBOARD = 60
TTL_ALERTAS = 120
_DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _data(texto: str | None, padrao: date) -> date:
    try:
        return date.fromisoformat(texto) if texto else padrao
    except ValueError:
        return padrao


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


async def _montar_dashboard(client: MeuERPClient, inicio: date, fim: date) -> dict:
    comp_ini, comp_fim, rotulo = _periodo_comparacao(inicio, fim)
    tarefas = [
        vendas.resumo(client, inicio, fim),
        vendas.resumo(client, comp_ini, comp_fim),
        vendas.vendas_por_dia(client, inicio, fim),
        vendas.formas_pagamento(client, inicio, fim),
        vendas.cancelamentos(client, inicio, fim),
    ]
    if inicio == fim:
        tarefas.append(vendas.vendas_por_hora(client, inicio))
    resultados = await asyncio.gather(*tarefas)
    atual, anterior, por_dia, formas, cancel = resultados[:5]
    por_hora = resultados[5] if inicio == fim else None

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
        "semFormaPagamento": sem_pagamento if sem_pagamento > 1 else 0,
        "cancelamentos": cancel,
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
            TTL_DASHBOARD,
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


async def _montar_alertas(client: MeuERPClient, cnpj: str, loja: int, hoje: date) -> dict:
    estoque, fiscal, pagar, receber, ruptura, venda_dia = await asyncio.gather(
        _secao("estoque", cnpj, alertas.estoque(client, loja)),
        _secao("fiscal", cnpj, alertas.notas_rejeitadas(client, hoje)),
        _secao("pagar", cnpj, alertas.contas(client, "pagar", hoje)),
        _secao("receber", cnpj, alertas.contas(client, "receber", hoje)),
        _secao("ruptura", cnpj, alertas.ruptura(client, loja, hoje)),
        _secao("venda típica", cnpj, alertas.venda_tipica_dia(client, hoje)),
    )
    return {
        "estoque": estoque, "notasRejeitadas": fiscal, "contasPagar": pagar, "contasReceber": receber,
        "ruptura": ruptura, "vendaTipicaDia": venda_dia,
    }


@router.get("/api/produtos/alertas")
async def alertas_mobile(request: Request, loja: int = Query(...), hoje: str | None = Query(None)):
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    data_hoje = _data(hoje, date.today())
    client = MeuERPClient(tenant["api_token"])
    return await cached(
        (tenant["cnpj"], "alertas", loja, data_hoje),
        TTL_ALERTAS,
        lambda: _montar_alertas(client, tenant["cnpj"], loja, data_hoje),
        guardar_se=lambda resultado: all(secao is not None for secao in resultado.values()),
    )
