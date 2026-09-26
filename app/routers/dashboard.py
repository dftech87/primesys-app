import sqlite3
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.meuerp_client import MeuERPClient
from app.tenants import get_tenant_by_cnpj

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _current_tenant(request: Request) -> sqlite3.Row | None:
    cnpj = request.session.get("cnpj")
    if not cnpj:
        return None
    return get_tenant_by_cnpj(cnpj)


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    tenant = _current_tenant(request)
    if tenant is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html", {"empresa_nome": tenant["nome_fantasia"]})


@router.get("/api/dashboard/summary")
async def dashboard_summary(
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
        data_inicio = date.fromisoformat(inicio) if inicio else data_fim - timedelta(days=30)
    except ValueError:
        data_inicio = data_fim - timedelta(days=30)

    client = MeuERPClient(tenant["api_token"])

    async def safe_get(path: str, params: dict) -> dict:
        try:
            return await client.get(path, params=params)
        except httpx.HTTPStatusError:
            return {"items": []}

    async def safe_get_all_pages(path: str, params: dict, max_pages: int = 5) -> list[dict]:
        """Percorre as páginas da API (limite máximo real é 100/página) até acabar
        ou até max_pages, para não estourar o rate limit em contas com muito volume."""
        items: list[dict] = []
        page = 1
        while page <= max_pages:
            resultado = await safe_get(path, {**params, "page": page, "limit": 100})
            page_items = resultado.get("items") or []
            items.extend(page_items)
            if not resultado.get("hasNext"):
                break
            page += 1
        return items

    receber = await safe_get(
        "/api/conta-receber/pendentes/v1",
        {"inicio": data_inicio.isoformat(), "fim": data_fim.isoformat(), "limit": 100},
    )
    pagar = await safe_get(
        "/api/conta-pagar/pendentes/v1",
        {"inicio": data_inicio.isoformat(), "fim": data_fim.isoformat(), "limit": 100},
    )

    # A empresa pode vender por NF-e (55), NFC-e (65) e/ou Pré-Venda (PV, venda de
    # balcão ainda não fiscalizada); somamos os três para bater com o valor que o
    # próprio Meu ERP Online mostra no painel de Vendas dele.
    # DataFim sem horário é tratado pela API como 00:00:00 daquele dia (exclui o dia
    # inteiro); por isso usamos o início do dia seguinte como limite superior.
    vendas_fim_exclusivo = data_fim + timedelta(days=1)
    vendas_por_mercadoria: dict[int, dict] = {}
    total_vendido = 0.0
    for modelo in ("55", "65", "PV"):
        itens_modelo = await safe_get_all_pages(
            "/api/documento/mercadorias-vendidas/v1",
            {
                "Modelo": modelo,
                "DataInicio": data_inicio.isoformat(),
                "DataFim": vendas_fim_exclusivo.isoformat(),
            },
        )
        for item in itens_modelo:
            chave = item.get("idMercadoriaVariacao") or item.get("descricao")
            acumulado = vendas_por_mercadoria.setdefault(
                chave, {"descricao": item.get("descricao"), "qtd": 0, "valor": 0.0}
            )
            acumulado["qtd"] += item.get("qtd") or 0
            acumulado["valor"] += item.get("valTotalLiquido") or 0
            total_vendido += item.get("valTotalLiquido") or 0

    mercadorias_vendidas = sorted(
        vendas_por_mercadoria.values(), key=lambda i: i["qtd"], reverse=True
    )[:10]

    receber_items = receber.get("items") or []
    pagar_items = pagar.get("items") or []

    total_receber = sum(item.get("valSaldo") or 0 for item in receber_items)
    total_pagar = sum(item.get("valSaldo") or 0 for item in pagar_items)

    proximos = sorted(receber_items, key=lambda i: i.get("dtVencimento") or "")[:8]

    return {
        "periodo": {"inicio": data_inicio.isoformat(), "fim": data_fim.isoformat()},
        "total_receber": total_receber,
        "total_pagar": total_pagar,
        "total_vendido": total_vendido,
        "proximos_recebiveis": [
            {"vencimento": i.get("dtVencimento"), "pessoa": i.get("nome"), "valor": i.get("valSaldo")}
            for i in proximos
        ],
        "mercadorias_vendidas": [
            {"descricao": i["descricao"], "quantidade": i["qtd"]} for i in mercadorias_vendidas
        ],
    }
