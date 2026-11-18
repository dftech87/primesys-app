"""Executa SQL de leitura no banco do cliente via /api/consulta/sql/v1.

A API só aceita o texto do SQL (sem parâmetros), então qualquer valor interpolado
precisa passar por `d()` (data) ou `i()` (inteiro). Nunca interpole texto vindo
do usuário: todas as consultas do app são modelos fixos no servidor.
"""

import re
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


# Regra antiga: a empresa de menor código que tem produtos. Certa quando o banco do ERP tem uma empresa só (o caso de hoje).
EMPRESA_DE_MENOR_CODIGO = "(select min(_idempresa) from mercadoria_variacao_empresa)"
_SO_DIGITOS = re.compile(r"^(\d{11}|\d{14})$")


def empresa(cnpj: str | None) -> str:
    """Código (_idempresa) da empresa deste CNPJ no banco do ERP, como trecho de SQL. Um banco pode ter mais de uma
    empresa (loja) da mesma organização; sem isto, a segunda loja veria produtos, preços e estoque da primeira.
    Se o CNPJ não estiver cadastrado na tabela de empresas (ou não vier), vale a regra antiga: nada muda para quem tem
    uma empresa só. O CNPJ só entra no SQL depois de conferido que são 11 ou 14 dígitos (nunca texto livre)."""
    digitos = re.sub(r"\D", "", str(cnpj or ""))
    if not _SO_DIGITOS.match(digitos):
        return EMPRESA_DE_MENOR_CODIGO
    return (f"coalesce((select min(_idempresa) from empresa where regexp_replace(coalesce(cnpjcpf, ''), '[^0-9]', '', 'g') = '{digitos}'), "
            f"{EMPRESA_DE_MENOR_CODIGO})")


async def executar(client: MeuERPClient, sql: str, max_paginas: int = 5) -> list[dict]:
    linhas: list[dict] = []
    for pagina in range(max_paginas):
        try:
            resposta = await client.post(
                f"/api/consulta/sql/v1?limit={TAMANHO_PAGINA}&offset={pagina * TAMANHO_PAGINA}",
                json={"sql": sql},
            )
        except (httpx.HTTPError, ValueError) as erro:   # ValueError: resposta que não é JSON (página de manutenção)
            raise ConsultaIndisponivel(str(erro)) from erro
        linhas.extend(resposta.get("items") or [])
        if not resposta.get("hasNext"):
            break
    return linhas
