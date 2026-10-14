"""Cópia do catálogo de cada empresa no servidor: produtos (com preço e regras de desconto), estoque do local padrão
e clientes. O servidor lê do ERP uma vez para todos os vendedores daquela empresa e cada celular baixa daqui só o
que mudou. Assim o número de vendedores não aumenta o gasto da cota de 20 chamadas por minuto do ERP.

Leitura do ERP:
  - completa de tempos em tempos (pega tudo, e o que sumiu do ERP fica inativo na cópia);
  - nos intervalos, só o que mudou desde a última marca de alteração do ERP.
Toda consulta é um modelo fixo; só entram números (i()) e marcas de data já validadas (_marca()).
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import timedelta

from app.cache import cached
from app.database import db_session
from app.meuerp_client import MeuERPClient
from app.sqlquery import executar, i
from vendas.db import agora_texto, carimbo_agora, texto_para_data

logger = logging.getLogger(__name__)

# De quanto em quanto tempo ler o que mudou / ler tudo de novo, por tabela.
INTERVALO_MUDANCAS = {"produtos": timedelta(minutes=10), "estoque": timedelta(minutes=3), "clientes": timedelta(minutes=15)}
INTERVALO_COMPLETA = {"produtos": timedelta(hours=24), "estoque": timedelta(hours=6), "clientes": timedelta(hours=24)}
MAX_PAGINAS = 400   # 40 mil linhas por leitura (100 por página, teto da API)
TABELAS = ("produtos", "estoque", "clientes")

_ATIVO = "coalesce({a}.flaginativo, 'F') not in ('T', 'S')"
_FORMATO_MARCA = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?$")


def _marca(texto: str) -> str:
    """Marca de alteração vinda do próprio ERP, conferida antes de entrar no SQL (nunca texto livre)."""
    if not _FORMATO_MARCA.match(texto or ""):
        raise ValueError("marca de alteração inválida")
    return f"'{texto}'"


async def _parametros(client: MeuERPClient) -> dict:
    """Empresa, local de estoque padrão e tabela de preço padrão (os de menor código ativos)."""
    linhas = await executar(client, f"""
        select (select min(_idlocal) from local_estoque l where {_ATIVO.format(a='l')}) as local,
               (select min(_idempresa) from mercadoria_variacao_empresa) as empresa,
               (select min(_idtabela) from tabela_preco_empresa t where {_ATIVO.format(a='t')}) as tabela""", max_paginas=1)
    p = linhas[0] if linhas else {}
    return {"empresa": int(p.get("empresa") or 1), "local": int(p.get("local") or 1), "tabela": int(p.get("tabela") or 1)}


# Só entra no catálogo do app o que dá para vender: produto ativo e com preço na tabela padrão. Na leitura completa o
# filtro é feito no próprio ERP (em clientes com dezenas de milhares de itens, lê bem menos páginas). Na leitura do que
# mudou vêm todos os alterados, e o que deixou de ser vendável chega como inativo (o celular tira da lista).
_VENDAVEL = f"{_ATIVO.format(a='ve')} and coalesce(p.valpreco, 0) > 0"


def _sql_produtos(par: dict, desde: str | None) -> str:
    alterado = "greatest(v.dtalteracao, m.dtalteracao, ve.dtalteracao, p.dtalteracao)"
    filtro = f"where {alterado} >= {_marca(desde)}" if desde else f"where {_VENDAVEL}"
    return f"""
        select v._idmercadoriavariacao as id, trim(m.descricao || ' ' || coalesce(v.descricao, '')) as descricao, coalesce(v.codigobarras, '') as barras, coalesce(v.embalagem, '') as embalagem,
               p.valpreco as preco, coalesce(ve.flagpermitirdesconto, 'T') as permite, ve.permaximodesconto as desconto_max,
               case when {_VENDAVEL} then 1 else 0 end as ativo, {alterado} as alterado
        from mercadoria_variacao v
        join mercadoria m on m._idmercadoria = v.idmercadoria
        join mercadoria_variacao_empresa ve on ve._idmercadoriavariacao = v._idmercadoriavariacao and ve._idempresa = {i(par['empresa'])}
        left join mercadoria_tabela_preco p on p._idmercadoriavariacao = v._idmercadoriavariacao
             and p._idempresa = {i(par['empresa'])} and p._idtabela = {i(par['tabela'])} and {_ATIVO.format(a='p')}
        {filtro}
        order by v._idmercadoriavariacao"""


def _sql_estoque(par: dict, desde: str | None) -> str:
    """Estoque do local padrão, só dos produtos vendáveis (mesma regra do catálogo)."""
    filtro = f"and e.datahora >= {_marca(desde)}" if desde else ""
    return f"""
        select e._idmercadoriavariacao as id, coalesce(e.qtdsaldo, 0) as saldo, e.datahora as alterado
        from mercadoria_estoque e
        join mercadoria_variacao_empresa ve on ve._idmercadoriavariacao = e._idmercadoriavariacao and ve._idempresa = e._idempresa
        join mercadoria_tabela_preco p on p._idmercadoriavariacao = e._idmercadoriavariacao and p._idempresa = e._idempresa
             and p._idtabela = {i(par['tabela'])} and {_ATIVO.format(a='p')}
        where e._idempresa = {i(par['empresa'])} and e._idlocalestoque = {i(par['local'])} and {_VENDAVEL} {filtro}
        order by e._idmercadoriavariacao"""


def _sql_clientes(par: dict, desde: str | None) -> str:
    filtro = f"where p.dataalteracao >= {_marca(desde)}" if desde else ""
    return f"""
        select p._idpessoa as id, p.nome, coalesce(p.sobrenome, '') as fantasia, coalesce(p.cnpjcpf, '') as documento,
               coalesce(en.bairro, '') as bairro, coalesce(mu.nome, '') as cidade, coalesce(mu.uf, en.uf, '') as uf,
               coalesce(nullif(co.celular, ''), nullif(co.fone, ''), '') as fone,
               case when coalesce(p.flaginativo, 'F') <> 'T' and p.tipo in ('C', 'A') then 1 else 0 end as ativo,
               p.dataalteracao as alterado
        from pessoa p
        left join lateral (select e.bairro, e.idibgemunicipio, e.uf from pessoa_endereco e
                           where e._idpessoa = p._idpessoa and coalesce(e.flaginativo, 'F') <> 'T'
                           order by (e.flagprincipal = 'T') desc, e._idendereco limit 1) en on true
        left join municipio mu on mu._idmunicipio = en.idibgemunicipio
        left join lateral (select c.celular, c.fone from pessoa_contato c where c._idpessoa = p._idpessoa limit 1) co on true
        {filtro}
        order by p._idpessoa"""


def _limpar_texto(v) -> str:
    v = (v or "").strip()
    return "" if v.lower() == "null" else " ".join(v.split())


def _documento(doc: str) -> str:
    """CNPJ vai inteiro (dado público da empresa). CPF vai mascarado: o vendedor reconhece, mas não circula completo."""
    d = re.sub(r"\D", "", doc or "")
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"***.{d[3:6]}.{d[6:9]}-**"
    return ""


def _num(v) -> float | None:
    return None if v is None else round(float(v), 4)


# Colunas gravadas na cópia, por tabela, e como cada linha do ERP vira essas colunas.
COLUNAS = {
    "produtos": ("descricao", "barras", "embalagem", "preco", "permite_desconto", "desconto_max", "ativo"),
    "estoque": ("saldo",),
    "clientes": ("nome", "fantasia", "documento", "bairro", "cidade", "uf", "fone", "ativo"),
}


def _converter(tabela: str, l: dict) -> dict:
    if tabela == "produtos":
        return {"descricao": _limpar_texto(l["descricao"]), "barras": _limpar_texto(l["barras"]), "embalagem": _limpar_texto(l["embalagem"]),
                "preco": _num(l["preco"]), "permite_desconto": 0 if l["permite"] == "F" else 1,
                "desconto_max": _num(l["desconto_max"]) or None, "ativo": int(l["ativo"])}
    if tabela == "estoque":
        return {"saldo": _num(l["saldo"]) or 0.0}
    return {"nome": _limpar_texto(l["nome"]), "fantasia": _limpar_texto(l["fantasia"]), "documento": _documento(l["documento"]),
            "bairro": _limpar_texto(l["bairro"]).title(), "cidade": _limpar_texto(l["cidade"]).title(), "uf": _limpar_texto(l["uf"]).upper(),
            "fone": _limpar_texto(l["fone"]), "ativo": int(l["ativo"])}


def _resumo(valores: dict) -> str:
    return hashlib.sha1(json.dumps(valores, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _gravar(cnpj: str, tabela: str, linhas: list[dict], completa: bool) -> int:
    """Grava na cópia só o que mudou (todas com o mesmo carimbo). Na leitura completa, o que não veio do ERP
    fica inativo (estoque: zerado). Devolve quantas linhas mudaram."""
    tab = f"pv_{tabela}"   # nome fixo, de TABELAS
    colunas = COLUNAS[tabela]
    carimbo = carimbo_agora()
    with db_session() as conn:
        atuais = {int(r["id"]): r["resumo"] for r in conn.execute(f"SELECT id, resumo FROM {tab} WHERE cnpj = ?", (cnpj,))}
        vistos, mudou = set(), 0
        sql = (f"INSERT INTO {tab} (cnpj, id, {', '.join(colunas)}, resumo, carimbo) VALUES ({', '.join(['?'] * (len(colunas) + 4))}) "
               f"ON CONFLICT (cnpj, id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in colunas)}, "
               f"resumo = excluded.resumo, carimbo = excluded.carimbo")
        for l in linhas:
            ident = int(l["id"])
            vistos.add(ident)
            valores = _converter(tabela, l)
            resumo = _resumo(valores)
            if atuais.get(ident) == resumo:
                continue
            conn.execute(sql, (cnpj, ident, *[valores[c] for c in colunas], resumo, carimbo))
            mudou += 1
        if completa:
            sumidos = [ident for ident in atuais if ident not in vistos]
            zerar = {"saldo": 0.0} if tabela == "estoque" else {"ativo": 0}
            for ident in sumidos:
                atual = dict(conn.execute(f"SELECT {', '.join(colunas)} FROM {tab} WHERE cnpj = ? AND id = ?", (cnpj, ident)).fetchone())
                atual.update(zerar)
                resumo = _resumo(atual)
                if resumo != atuais[ident]:
                    conn.execute(f"UPDATE {tab} SET {', '.join(f'{c} = ?' for c in zerar)}, resumo = ?, carimbo = ? WHERE cnpj = ? AND id = ?",
                                 (*zerar.values(), resumo, carimbo, cnpj, ident))
                    mudou += 1
    return mudou


def _controle(cnpj: str) -> dict[str, dict]:
    with db_session() as conn:
        linhas = conn.execute("SELECT * FROM pv_sincronizacao WHERE cnpj = ?", (cnpj,)).fetchall()
    return {r["tabela"]: dict(r) for r in linhas}


def _salvar_controle(cnpj: str, tabela: str, **campos) -> None:
    with db_session() as conn:
        conn.execute("INSERT INTO pv_sincronizacao (cnpj, tabela) VALUES (?, ?) ON CONFLICT (cnpj, tabela) DO NOTHING", (cnpj, tabela))
        for coluna, valor in campos.items():   # colunas fixas deste módulo
            conn.execute(f"UPDATE pv_sincronizacao SET {coluna} = ? WHERE cnpj = ? AND tabela = ?", (valor, cnpj, tabela))


def _vencido(texto: str, intervalo: timedelta) -> bool:
    return not texto or texto_para_data(agora_texto()) - texto_para_data(texto) >= intervalo


async def _atualizar(tenant: dict) -> None:
    cnpj = tenant["cnpj"]
    client = MeuERPClient(tenant["api_token"], segundo_plano=True)
    par = await _parametros(client)
    construtores = {"produtos": _sql_produtos, "estoque": _sql_estoque, "clientes": _sql_clientes}
    controle = _controle(cnpj)
    for tabela in TABELAS:
        c = controle.get(tabela, {})
        completa = _vencido(c.get("ultima_completa", ""), INTERVALO_COMPLETA[tabela]) or not c.get("marca_erp")
        if not completa and not _vencido(c.get("ultima_leitura", ""), INTERVALO_MUDANCAS[tabela]):
            continue
        try:
            linhas = await executar(client, construtores[tabela](par, None if completa else c["marca_erp"]), max_paginas=MAX_PAGINAS)
            mudou = await asyncio.to_thread(_gravar, cnpj, tabela, linhas, completa)
            marcas = [str(l["alterado"]) for l in linhas if l.get("alterado")]
            nova_marca = max(marcas + [c.get("marca_erp", "")]) if marcas else c.get("marca_erp", "")
            agora = agora_texto()
            _salvar_controle(cnpj, tabela, marca_erp=nova_marca, ultima_leitura=agora, erro="",
                             **({"ultima_completa": agora} if completa else {}))
            logger.info("catálogo %s/%s: %s, %d lidas, %d mudaram", cnpj, tabela, "completa" if completa else "mudanças", len(linhas), mudou)
        except Exception as erro:
            logger.exception("falha lendo %s do ERP (empresa %s)", tabela, cnpj)
            _salvar_controle(cnpj, tabela, erro=type(erro).__name__)


REGRA_DO_CATALOGO = "vendavel-v1"   # mude ao trocar a regra de quais produtos entram: força uma leitura completa


def aplicar_regra_do_catalogo() -> None:
    """Quando a regra muda (ex.: passar a trazer só produto ativo com preço), a próxima sincronização de cada empresa
    faz leitura completa de produtos e estoque, e o que saiu da regra vira inativo nos celulares."""
    with db_session() as conn:
        marcada = conn.execute("SELECT marca_erp FROM pv_sincronizacao WHERE cnpj = '*' AND tabela = 'regra'").fetchone()
        if marcada and marcada["marca_erp"] == REGRA_DO_CATALOGO:
            return
        conn.execute("UPDATE pv_sincronizacao SET ultima_completa = '' WHERE tabela IN ('produtos', 'estoque')")
        conn.execute("INSERT INTO pv_sincronizacao (cnpj, tabela, marca_erp) VALUES ('*', 'regra', ?) "
                     "ON CONFLICT (cnpj, tabela) DO UPDATE SET marca_erp = excluded.marca_erp", (REGRA_DO_CATALOGO,))
    logger.warning("regra do catálogo mudou para %s: próxima sincronização lê tudo de novo", REGRA_DO_CATALOGO)


_em_andamento: dict[str, asyncio.Task] = {}


def disparar(tenant: dict) -> bool:
    """Começa (em segundo plano) a leitura do ERP desta empresa, se ainda não estiver rodando. Devolve se está rodando."""
    cnpj = tenant["cnpj"]
    tarefa = _em_andamento.get(cnpj)
    if tarefa is None or tarefa.done():
        tarefa = asyncio.create_task(_atualizar(tenant))
        tarefa.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        _em_andamento[cnpj] = tarefa
    return not tarefa.done()


def estado(cnpj: str) -> dict:
    controle = _controle(cnpj)
    with db_session() as conn:
        totais = {t: conn.execute(f"SELECT count(*) AS n FROM pv_{t} WHERE cnpj = ?" + (" AND ativo = 1" if t != "estoque" else ""),
                                  (cnpj,)).fetchone()["n"] for t in TABELAS}
    tarefa = _em_andamento.get(cnpj)
    return {
        "lendo_erp": bool(tarefa and not tarefa.done()),
        "tabelas": {t: {"total": int(totais[t]), "pronta": bool(controle.get(t, {}).get("ultima_completa")),
                        "erro": controle.get(t, {}).get("erro", "")} for t in TABELAS},
    }


async def _estoque_agora(token: str, cnpj: str, id_produto: int) -> list[dict]:
    client = MeuERPClient(token)
    locais = await cached((cnpj, "vendas-locais-estoque"), 6 * 3600,
                          lambda: client.get("/api/local-estoque/v1", params={"limit": 100}))
    resultado = []
    for local in (locais.get("items") or []):
        if local.get("ativo") != "S":
            continue
        try:
            e = await client.get(f"/api/mercadoria/{int(id_produto)}/local-estoque/{int(local['codigo'])}/estoque/v1")
            saldo = float(e.get("qtdSaldo") or 0)
        except Exception as erro:   # 404 = produto nunca movimentado neste local: estoque zero
            resposta = getattr(erro, "response", None)
            saldo = 0.0 if resposta is not None and resposta.status_code == 404 else None
        resultado.append({"local": int(local["codigo"]), "descricao": local.get("descricao") or "", "saldo": saldo})
    return resultado


async def estoque_agora(tenant: dict, id_produto: int) -> list[dict] | None:
    """Estoque do produto agora, em cada local (consulta o ERP só quando o vendedor pede; guarda 1 minuto)."""
    with db_session() as conn:
        existe = conn.execute("SELECT 1 FROM pv_produtos WHERE cnpj = ? AND id = ?", (tenant["cnpj"], int(id_produto))).fetchone()
    if not existe:
        return None
    return await cached((tenant["cnpj"], "vendas-estoque-agora", int(id_produto)), 60,
                        lambda: _estoque_agora(tenant["api_token"], tenant["cnpj"], int(id_produto)))


def mudancas(cnpj: str, tabela: str, cursor: str, limite: int = 2000) -> dict:
    """Linhas da cópia que mudaram depois do cursor que o celular guardou ('carimbo|id' da última linha recebida),
    em ordem. Uma leitura completa grava milhares de linhas com o mesmo carimbo, por isso o id entra no cursor."""
    colunas = COLUNAS[tabela]
    carimbo, _, ultimo_id = (cursor or "").partition("|")
    ultimo_id = int(ultimo_id) if ultimo_id.isdigit() else 0
    with db_session() as conn:
        linhas = conn.execute(
            f"SELECT id, {', '.join(colunas)}, carimbo FROM pv_{tabela} WHERE cnpj = ? AND (carimbo > ? OR (carimbo = ? AND id > ?)) "
            f"ORDER BY carimbo, id LIMIT ?",
            (cnpj, carimbo, carimbo, ultimo_id, limite + 1)).fetchall()
    linhas = [dict(l) for l in linhas]
    mais = len(linhas) > limite
    linhas = linhas[:limite]
    novo_cursor = f"{linhas[-1]['carimbo']}|{linhas[-1]['id']}" if linhas else (cursor or "")
    for l in linhas:
        l.pop("carimbo")
    return {"itens": linhas, "cursor": novo_cursor, "mais": mais}
