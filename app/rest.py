"""Auxiliares para os endpoints REST (paginados) da API do Meu ERP Online."""

from app.meuerp_client import MeuERPClient


async def todas_paginas(
    client: MeuERPClient, path: str, params: dict, max_paginas: int = 5
) -> tuple[list[dict], bool]:
    """Percorre as páginas (100 por vez). Erros sobem para quem chamou.

    Devolve (itens, parcial); `parcial` é True quando parou no limite de páginas
    e ainda havia mais registros.
    """
    itens: list[dict] = []
    for pagina in range(1, max_paginas + 1):
        resposta = await client.get(path, params={**params, "page": pagina, "limit": 100})
        itens.extend(resposta.get("items") or [])
        if not resposta.get("hasNext"):
            return itens, False
    return itens, True
