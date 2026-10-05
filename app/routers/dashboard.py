import asyncio
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import vendas
from app.cache import cached
from app.deps import current_tenant
from app.meuerp_client import MeuERPClient
from app.rest import safe_get
from app.sqlquery import ConsultaIndisponivel

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

TTL_VENDAS = 60            # período que inclui hoje
TTL_VENDAS_ENCERRADO = 900  # período já fechado
TTL_CONTAS = 300


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    tenant = current_tenant(request)
    if tenant is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"empresa_nome": tenant["nome_fantasia"]},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/dashboard/summary")
async def dashboard_summary(
    request: Request,
    inicio: str | None = Query(default=None),
    fim: str | None = Query(default=None),
):
    tenant = current_tenant(request)
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

    async def vendas_do_periodo() -> dict:
        resumo, top = await asyncio.gather(
            vendas.resumo(client, data_inicio, data_fim),
            vendas.top_produtos(client, data_inicio, data_fim, 10),
        )
        return {"resumo": resumo, "top": top}

    try:
        dados_vendas, receber, pagar = await asyncio.gather(
            cached(
                (tenant["cnpj"], "web-vendas", data_inicio, data_fim),
                TTL_VENDAS if data_fim >= hoje else TTL_VENDAS_ENCERRADO,
                vendas_do_periodo,
            ),
            cached(
                (tenant["cnpj"], "web-receber", data_inicio, data_fim),
                TTL_CONTAS,
                lambda: safe_get(
                    client,
                    "/api/conta-receber/pendentes/v1",
                    {"inicio": data_inicio.isoformat(), "fim": data_fim.isoformat(), "limit": 100},
                ),
            ),
            cached(
                (tenant["cnpj"], "web-pagar", data_inicio, data_fim),
                TTL_CONTAS,
                lambda: safe_get(
                    client,
                    "/api/conta-pagar/pendentes/v1",
                    {"inicio": data_inicio.isoformat(), "fim": data_fim.isoformat(), "limit": 100},
                ),
            ),
        )
    except ConsultaIndisponivel:
        logger.exception("consulta SQL indisponível (dashboard web) para %s", tenant["cnpj"])
        return JSONResponse({"detail": "Consulta indisponível para esta empresa no momento."}, status_code=502)

    receber_items = receber.get("items") or []
    pagar_items = pagar.get("items") or []
    proximos = sorted(receber_items, key=lambda i: i.get("dtVencimento") or "")[:8]

    return {
        "periodo": {"inicio": data_inicio.isoformat(), "fim": data_fim.isoformat()},
        "total_receber": sum(item.get("valSaldo") or 0 for item in receber_items),
        "total_pagar": sum(item.get("valSaldo") or 0 for item in pagar_items),
        "total_vendido": dados_vendas["resumo"]["total"],
        "proximos_recebiveis": [
            {"vencimento": i.get("dtVencimento"), "pessoa": i.get("nome"), "valor": i.get("valSaldo")}
            for i in proximos
        ],
        "mercadorias_vendidas": [
            {"descricao": p["descricao"], "quantidade": p["quantidade"]} for p in dados_vendas["top"]
        ],
    }
