"""Memória do servidor que sobrevive a uma publicação (reinício): resultados de consultas ao ERP que mudam pouco
(quem é cada vendedor, permissões do perfil, nome do funcionário). Primeiro procura na memória do processo, depois
no banco (tabela pv_cache) e só então pergunta ao ERP. Assim o login logo depois de uma publicação não precisa
refazer meia dúzia de consultas. Fica só no servidor: nada daqui vai para o celular.
"""

import json
from datetime import timedelta

from app.cache import cached, descartar
from app.database import db_session
from vendas.db import agora_texto, texto_para_data


async def guardado(cnpj: str, nome: str, ttl_segundos: int, produzir):
    async def do_banco_ou_erp():
        with db_session() as conn:
            linha = conn.execute("SELECT valor, ate FROM pv_cache WHERE cnpj = ? AND chave = ?", (cnpj, nome)).fetchone()
        if linha and texto_para_data(linha["ate"]) > texto_para_data(agora_texto()):
            return json.loads(linha["valor"])
        valor = await produzir()
        ate = (texto_para_data(agora_texto()) + timedelta(seconds=ttl_segundos)).strftime("%Y-%m-%d %H:%M:%S")
        with db_session() as conn:
            conn.execute("INSERT INTO pv_cache (cnpj, chave, valor, ate) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT (cnpj, chave) DO UPDATE SET valor = excluded.valor, ate = excluded.ate",
                         (cnpj, nome, json.dumps(valor, ensure_ascii=False), ate))
        return valor

    return await cached((cnpj, "vendas-guardado", nome), ttl_segundos, do_banco_ou_erp)


def gravar(cnpj: str, nome: str, valor, ttl_segundos: int) -> None:
    """Grava um valor novo (renovação em segundo plano) no banco e na memória do processo."""
    ate = (texto_para_data(agora_texto()) + timedelta(seconds=ttl_segundos)).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as conn:
        conn.execute("INSERT INTO pv_cache (cnpj, chave, valor, ate) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (cnpj, chave) DO UPDATE SET valor = excluded.valor, ate = excluded.ate",
                     (cnpj, nome, json.dumps(valor, ensure_ascii=False), ate))
    descartar((cnpj, "vendas-guardado", nome))


def vence_em(cnpj: str, nome: str) -> float:
    """Segundos até vencer (negativo se já venceu ou não existe)."""
    with db_session() as conn:
        linha = conn.execute("SELECT ate FROM pv_cache WHERE cnpj = ? AND chave = ?", (cnpj, nome)).fetchone()
    if not linha:
        return -1
    return (texto_para_data(linha["ate"]) - texto_para_data(agora_texto())).total_seconds()


def esquecer(cnpj: str, nome: str) -> None:
    descartar((cnpj, "vendas-guardado", nome))
    with db_session() as conn:
        conn.execute("DELETE FROM pv_cache WHERE cnpj = ? AND chave = ?", (cnpj, nome))
