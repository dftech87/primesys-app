import asyncio
import sqlite3
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.meuerp_client import MeuERPClient
from app.tenants import get_tenant_by_cnpj
from app.vendas import vendas_por_dia

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MAX_RESULTADOS_LISTA = 20


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
    return templates.TemplateResponse(
        request,
        "produtos.html",
        {"empresa_nome": tenant["nome_fantasia"]},
        headers={"Cache-Control": "no-store"},
    )


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
        resultado = await client.get("/api/mercadoria/v1", params={"filtro": q, "limit": MAX_RESULTADOS_LISTA})
    except httpx.HTTPStatusError:
        return JSONResponse({"detail": "erro ao consultar produtos"}, status_code=502)

    itens = resultado.get("items") or []

    # Preço e estoque não entram aqui de propósito: buscá-los por item deixaria a
    # busca em uma única chamada por resultado (até MAX_RESULTADOS_LISTA x 2), o que
    # estoura rápido o limite de 20 req/min da API e deixa a busca lenta. Eles só
    # são consultados na tela de detalhe, ao tocar em um produto específico.
    produtos = [
        {
            "idVariacao": item["codigoMercadoriaVariacao"],
            "descricao": item.get("descricao"),
            "codigoBarras": item.get("codigoBarras"),
            "codigoInterno": item.get("codigoMercadoria"),
            "unidade": item.get("embalagem"),
        }
        for item in itens
    ]
    return {"total": resultado.get("total"), "produtos": produtos}


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
async def api_dashboard_mobile(
    request: Request,
    inicio: str | None = Query(default=None),
    fim: str | None = Query(default=None),
):
    tenant = _current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    hoje = date.today()
    try:
        data_fim = date.fromisoformat(fim) if fim else hoje
    except ValueError:
        data_fim = hoje
    try:
        data_inicio = date.fromisoformat(inicio) if inicio else hoje
    except ValueError:
        data_inicio = hoje

    # DataFim sem horário é tratado pela API como 00:00:00 daquele dia (exclui o
    # dia inteiro); por isso usamos o início do dia seguinte como limite superior.
    fim_exclusivo = data_fim + timedelta(days=1)

    client = MeuERPClient(tenant["api_token"])
    por_dia = await vendas_por_dia(client, data_inicio.isoformat(), fim_exclusivo.isoformat())

    vendas_por_dia_lista = []
    cursor = data_inicio
    while cursor <= data_fim:
        chave = cursor.isoformat()
        vendas_por_dia_lista.append({"dia": cursor.strftime("%d/%m"), "valor": por_dia.get(chave, 0)})
        cursor += timedelta(days=1)

    return {
        "periodo": {"inicio": data_inicio.isoformat(), "fim": data_fim.isoformat()},
        "total": sum(por_dia.values()),
        "vendasPorDia": vendas_por_dia_lista,
    }
