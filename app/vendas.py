"""Consultas de vendas compartilhadas entre o dashboard web e o dashboard mobile.

A empresa pode vender por NF-e (55), NFC-e (65) e/ou Pré-Venda (PV, venda de
balcão ainda não fiscalizada); somamos os três para bater com o valor que o
próprio Meu ERP Online mostra no painel de Vendas dele.
"""

import httpx

from app.meuerp_client import MeuERPClient

MODELOS_VENDA = ("55", "65", "PV")


async def safe_get(client: MeuERPClient, path: str, params: dict) -> dict:
    try:
        return await client.get(path, params=params)
    except httpx.HTTPStatusError:
        return {"items": []}


async def safe_get_all_pages(client: MeuERPClient, path: str, params: dict, max_pages: int = 5) -> list[dict]:
    """Percorre as páginas da API (limite máximo real é 100/página) até acabar
    ou até max_pages, para não estourar o rate limit em contas com muito volume."""
    items: list[dict] = []
    page = 1
    while page <= max_pages:
        resultado = await safe_get(client, path, {**params, "page": page, "limit": 100})
        page_items = resultado.get("items") or []
        items.extend(page_items)
        if not resultado.get("hasNext"):
            break
        page += 1
    return items


async def total_vendido(client: MeuERPClient, data_inicio: str, data_fim_exclusivo: str) -> float:
    """Soma o valor líquido vendido no intervalo [data_inicio, data_fim_exclusivo)."""
    total = 0.0
    for modelo in MODELOS_VENDA:
        itens = await safe_get_all_pages(
            client,
            "/api/documento/mercadorias-vendidas/v1",
            {"Modelo": modelo, "DataInicio": data_inicio, "DataFim": data_fim_exclusivo},
        )
        total += sum(item.get("valTotalLiquido") or 0 for item in itens)
    return total


async def vendas_por_dia(client: MeuERPClient, data_inicio: str, data_fim_exclusivo: str) -> dict[str, float]:
    """Soma o valor total de documentos de venda por dia (YYYY-MM-DD) no intervalo.

    Usa /api/documento/v1 (não mercadorias-vendidas) porque precisamos da data de
    cada documento — o endpoint de mercadorias vendidas só devolve totais já
    agregados por produto, sem granularidade diária.
    """
    por_dia: dict[str, float] = {}
    for modelo in MODELOS_VENDA:
        itens = await safe_get_all_pages(
            client,
            "/api/documento/v1",
            {"Modelo": modelo, "DataInicio": data_inicio, "DataFim": data_fim_exclusivo},
        )
        for item in itens:
            data_hora = item.get("dataHora") or ""
            dia = data_hora[:10]
            if not dia:
                continue
            por_dia[dia] = por_dia.get(dia, 0) + (item.get("valTotal") or 0)
    return por_dia
