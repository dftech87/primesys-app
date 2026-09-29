import asyncio
import sqlite3
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.meuerp_client import MeuERPClient
from app.tenants import get_tenant_by_cnpj
from app.vendas import total_vendido, vendas_por_dia

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MAX_RESULTADOS_COM_PRECO = 8


def _current_tenant(request: Request) -> sqlite3.Row | None:
    cnpj = request.session.get("cnpj")
    if not cnpj:
        return None
    return get_tenant_by_cnpj(cnpj)


async def _preco_venda(client: MeuERPClient, id_variacao: int) -> float | None:
    try:
        resultado = await client.get(f"/api/mercadoria/{id_variacao}/tabelas-preco/v1", params={"limit": 10})
    except httpx.HTTPStatusError:
        return None
    ativas = [i for i in (resultado.get("items") or []) if i.get("ativo") == "S"]
    if not ativas:
        return None
    ativas.sort(key=lambda i: i.get("codigoTabela") or 0)
    return ativas[0].get("valPreco")


async def _estoque(client: MeuERPClient, id_variacao: int, id_loja: int) -> float | None:
    try:
        resultado = await client.get(
            f"/api/mercadoria/{id_variacao}/local-estoque/{id_loja}/estoque/v1"
        )
    except httpx.HTTPStatusError:
        return None
    return resultado.get("qtdSaldo")


@router.get("/produtos", response_class=HTMLResponse)
async def produtos_page(request: Request):
    tenant = _current_tenant(request)
    if tenant is None:
        return RedirectResponse("/login?next=/produtos", status_code=303)
    return templates.TemplateResponse(request, "produtos.html", {"empresa_nome": tenant["nome_fantasia"]})


@router.get("/api/produtos/lojas")
async def api_lojas(request: Request):
    tenant = _current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    client = MeuERPClient(tenant["api_token"])
    try:
        resultado = await client.get("/api/local-estoque/v1", params={"limit": 100})
    except httpx.HTTPStatusError:
        return JSONResponse({"detail": "erro ao consultar lojas"}, status_code=502)
    lojas = [
        {"codigo": item["codigo"], "descricao": item["descricao"]}
        for item in (resultado.get("items") or [])
        if item.get("ativo") == "S"
    ]
    return {"lojas": lojas}


@router.get("/api/produtos/buscar")
async def api_buscar(request: Request, q: str = Query(min_length=2), loja: int = Query(...)):
    tenant = _current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    client = MeuERPClient(tenant["api_token"])
    try:
        resultado = await client.get("/api/mercadoria/v1", params={"filtro": q, "limit": MAX_RESULTADOS_COM_PRECO})
    except httpx.HTTPStatusError:
        return JSONResponse({"detail": "erro ao consultar produtos"}, status_code=502)

    itens = resultado.get("items") or []

    async def montar(item: dict) -> dict:
        id_variacao = item["codigoMercadoriaVariacao"]
        preco, estoque = await asyncio.gather(
            _preco_venda(client, id_variacao), _estoque(client, id_variacao, loja)
        )
        return {
            "idVariacao": id_variacao,
            "descricao": item.get("descricao"),
            "codigoBarras": item.get("codigoBarras"),
            "codigoInterno": item.get("codigoMercadoria"),
            "unidade": item.get("embalagem"),
            "precoVenda": preco,
            "estoque": estoque,
        }

    produtos = await asyncio.gather(*(montar(item) for item in itens))
    return {"total": resultado.get("total"), "produtos": list(produtos)}


@router.get("/api/produtos/{id_variacao}/detalhe")
async def api_detalhe(request: Request, id_variacao: int, loja: int = Query(...)):
    tenant = _current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    client = MeuERPClient(tenant["api_token"])
    try:
        mercadoria = await client.get(f"/api/mercadoria/v1/{id_variacao}")
    except httpx.HTTPStatusError:
        return JSONResponse({"detail": "produto não encontrado"}, status_code=404)

    async def custo() -> float | None:
        try:
            resultado = await client.get(f"/api/mercadoria/{id_variacao}/custo/v1")
            return resultado.get("valCusto")
        except httpx.HTTPStatusError:
            return None

    custo_valor, preco, estoque = await asyncio.gather(
        custo(), _preco_venda(client, id_variacao), _estoque(client, id_variacao, loja)
    )

    return {
        "descricao": mercadoria.get("descricao"),
        "codigoBarras": mercadoria.get("codigoBarras"),
        "codigoInterno": mercadoria.get("codigoMercadoria"),
        "unidade": mercadoria.get("embalagem"),
        "ativo": mercadoria.get("ativo") == "S",
        "precoCusto": custo_valor,
        "precoVenda": preco,
        "estoque": estoque,
    }


@router.get("/api/produtos/dashboard-mobile")
async def api_dashboard_mobile(request: Request):
    tenant = _current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    client = MeuERPClient(tenant["api_token"])
    hoje = date.today()
    amanha = hoje + timedelta(days=1)
    inicio_semana = hoje - timedelta(days=6)
    inicio_mes = hoje.replace(day=1)
    inicio_ano = hoje.replace(month=1, day=1)

    # Sequencial de propósito: rodar as 5 consultas em paralelo soma muitas chamadas
    # de uma vez e estoura o limite de 20 req/min da API, travando a tela inteira.
    total_hoje = await total_vendido(client, hoje.isoformat(), amanha.isoformat())
    total_semana = await total_vendido(client, inicio_semana.isoformat(), amanha.isoformat())
    total_mes = await total_vendido(client, inicio_mes.isoformat(), amanha.isoformat())
    total_ano = await total_vendido(client, inicio_ano.isoformat(), amanha.isoformat())
    por_dia_mes = await vendas_por_dia(client, inicio_mes.isoformat(), amanha.isoformat())

    dias_do_mes = []
    cursor = inicio_mes
    while cursor <= hoje:
        chave = cursor.isoformat()
        dias_do_mes.append({"dia": cursor.strftime("%d/%m"), "valor": por_dia_mes.get(chave, 0)})
        cursor += timedelta(days=1)

    return {
        "hoje": total_hoje,
        "semana": total_semana,
        "mes": total_mes,
        "ano": total_ano,
        "vendasPorDia": dias_do_mes,
    }
