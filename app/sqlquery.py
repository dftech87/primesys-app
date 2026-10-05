"""Executa SQL de leitura no banco do cliente via /api/consulta/sql/v1.

A API só aceita o texto do SQL (sem parâmetros), então qualquer valor interpolado
precisa passar por `d()` (data) ou `i()` (inteiro). Nunca interpole texto vindo
do usuário: todas as consultas do app são modelos fixos no servidor.
"""

from datetime import date

import httpx

from app.meuerp_client import MeuERPClient

TAMANHO_PAGINA = 100  # teto real da API; pedir mais devolve HTTP 400


class ConsultaIndisponivel(Exception):
    """A consulta SQL falhou (API fora do ar, sem permissão, SQL recusado...)."""


def d(valor: date) -> str:
    """Literal de data ISO para o SQL. Só aceita `date` de verdade."""
    if not isinstance(valor, date):
        raise TypeError("d() só aceita datetime.date")
    return f"'{valor.isoformat()}'"


def i(valor) -> str:
    """Literal inteiro para o SQL (levanta ValueError se não for um inteiro)."""
    return str(int(valor))


async def executar(client: MeuERPClient, sql: str, max_paginas: int = 5) -> list[dict]:
    linhas: list[dict] = []
    for pagina in range(max_paginas):
        try:
            resposta = await client.post(
                f"/api/consulta/sql/v1?limit={TAMANHO_PAGINA}&offset={pagina * TAMANHO_PAGINA}",
                json={"sql": sql},
            )
        except httpx.HTTPError as erro:
            raise ConsultaIndisponivel(str(erro)) from erro
        linhas.extend(resposta.get("items") or [])
        if not resposta.get("hasNext"):
            break
    return linhas
