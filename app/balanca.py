"""Exportar para balança / Solicitar balança, na ficha do produto (o cliente escolhe Sim ou Não; nada é digitado).

Escreve no cadastro do ERP, por isso com várias travas:
  - quem pode: permissão 3901 (Exportação para balança) do perfil, conferida nas rotas (app/routers/balanca.py);
  - só os campos que mudaram vão ao ERP (PUT /api/mercadoria/v1/{variação}); provado na DFTECH que o resto do cadastro fica igual;
  - o estado é lido do ERP na hora (nunca do cache) e conferido de novo depois de gravar;
  - "Solicitar balança" faz o caixa pedir o peso: em produto que não é vendido por KG só passa com confirmação explícita;
  - tudo fica registrado aqui (o ERP grava como "API-Publica"): quem, quando, de quê para quê; dá para desfazer por 30 minutos.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.database import db_session
from app.meuerp_client import MeuERPClient
from app.sqlquery import ConsultaIndisponivel, executar, i

logger = logging.getLogger(__name__)

JANELA_DESFAZER = timedelta(minutes=30)
FORMATO_HORA = "%Y-%m-%d %H:%M:%S.%f"   # UTC, com frações de segundo
UNIDADES_DE_PESO = {"KG", "KGS"}

_SQL_ESTADO = """
    select coalesce(ve.flagexportarbalanca, 'F') as exportar, coalesce(ve.flagsolicitarbalanca, 'F') as solicitar,
           upper(trim(coalesce(v.embalagem, ''))) as unidade
    from mercadoria_variacao v
    left join mercadoria_variacao_empresa ve on ve._idmercadoriavariacao = v._idmercadoriavariacao
         and ve._idempresa = (select min(_idempresa) from mercadoria_variacao_empresa)
    where v._idmercadoriavariacao = {id}"""


class BalancaRecusada(Exception):
    """Motivo claro para a pessoa (vai direto para a tela); `extra` leva dados para a tela reagir."""

    def __init__(self, detalhe: str, status: int = 422, **extra):
        super().__init__(detalhe)
        self.detalhe, self.status, self.extra = detalhe, status, extra


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _sim(valor) -> bool:
    """O banco do ERP guarda 'T'/'F' (alguns cadastros antigos, 'S'/'N')."""
    return str(valor or "").strip().upper() in ("T", "S")


async def estado(client: MeuERPClient, id_variacao: int) -> dict:
    """Exportar, Solicitar e unidade do produto, lidos do ERP agora (uma consulta)."""
    try:
        linhas = await executar(client, _SQL_ESTADO.format(id=i(id_variacao)), max_paginas=1)
    except (ConsultaIndisponivel, ValueError) as erro:
        raise BalancaRecusada("Não foi possível ler a balança do produto agora. Tente de novo em instantes.", 502) from erro
    if not linhas:
        raise BalancaRecusada("Produto não encontrado.", 404)
    l = linhas[0]
    return {"exportar": _sim(l.get("exportar")), "solicitar": _sim(l.get("solicitar")), "unidade": str(l.get("unidade") or "")}


def _motivos(antes: dict, novo: dict, unidade: str) -> list[str]:
    """Motivos que exigem confirmar (o servidor confere; a tela só mostra o aviso). Só vale ao LIGAR o Solicitar:
    quem apenas mexe no Exportar de um produto que já tinha Solicitar ligado não é barrado por isso."""
    motivos = []
    if novo["solicitar"] and not antes["solicitar"] and unidade not in UNIDADES_DE_PESO:
        motivos.append("solicitar_sem_peso")
    return motivos


def _campos(antes: dict, novo: dict) -> dict:
    """Só o que mudou, no formato S/N que a API aceita."""
    return {chave: ("S" if novo[valor] else "N")
            for chave, valor in (("exportarBalanca", "exportar"), ("solicitarBalanca", "solicitar")) if novo[valor] != antes[valor]}


async def _gravar(client: MeuERPClient, id_variacao: int, campos: dict, esperado: dict) -> None:
    try:
        resposta = await client.put(f"/api/mercadoria/v1/{int(id_variacao)}", json=campos)
    except httpx.HTTPStatusError as erro:
        codigo = erro.response.status_code
        if codigo in (401, 403):
            raise BalancaRecusada("O ERP não liberou a alteração do cadastro para esta empresa (permissão do acesso do aplicativo).", 403)
        mensagem = ""
        try:
            mensagem = str(erro.response.json().get("mensagem") or "")[:200]
        except Exception:
            pass
        logger.warning("ERP recusou a alteração de balança (HTTP %s): %s", codigo, mensagem or erro.response.text[:200])
        raise BalancaRecusada(mensagem or "O ERP não aceitou a alteração. Tente de novo ou altere direto no ERP.", 502 if codigo >= 500 else 422)
    except httpx.HTTPError:
        # Sem resposta: pode ter gravado mesmo assim. Confere no ERP antes de dizer que falhou.
        try:
            if await estado(client, id_variacao) == esperado:
                logger.warning("o ERP demorou, mas a balança do produto %s foi gravada", id_variacao)
                return
        except (httpx.HTTPError, BalancaRecusada):
            pass
        raise BalancaRecusada("Não foi possível confirmar se o ERP gravou a balança. Reabra a ficha e confira antes de tentar de novo.", 502)
    if isinstance(resposta, dict) and resposta.get("status") is False:
        raise BalancaRecusada(str(resposta.get("mensagem") or "O ERP não aceitou a alteração.")[:200])


def _registrar(cnpj: str, email: str, nome: str, id_variacao: int, tipo: str, antes: dict, novo: dict) -> str | None:
    chave = uuid.uuid4().hex
    try:
        with db_session() as conn:
            conn.execute(
                "INSERT INTO alteracoes_balanca (id, cnpj, email, nome, id_variacao, tipo, exportar_antes, solicitar_antes, exportar_novo, solicitar_novo, criado_em)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chave, cnpj, email, nome, int(id_variacao), tipo, int(antes["exportar"]), int(antes["solicitar"]), int(novo["exportar"]), int(novo["solicitar"]),
                 _agora().strftime(FORMATO_HORA)),
            )
    except Exception:
        # A balança já foi gravada no ERP: não dá para desfazer isso aqui. Fica o alerta nos logs.
        logger.exception("ALTERAÇÃO DE BALANÇA SEM REGISTRO: %s %s produto %s %s -> %s", cnpj, email, id_variacao, antes, novo)
        return None
    return chave


async def alterar(client: MeuERPClient, cnpj: str, email: str, nome: str, id_variacao: int, exportar: bool, solicitar: bool,
                  confirmo: bool) -> dict:
    antes = await estado(client, id_variacao)
    novo = {"exportar": bool(exportar), "solicitar": bool(solicitar)}
    campos = _campos(antes, novo)
    if not campos:
        raise BalancaRecusada("Nada mudou: a balança já está assim.")
    motivos = _motivos(antes, novo, antes["unidade"])
    if motivos and not confirmo:
        raise BalancaRecusada("Confirme marcando a caixinha para salvar.", 409, confirmar=motivos)

    esperado = {**novo, "unidade": antes["unidade"]}
    await _gravar(client, id_variacao, campos, esperado)
    depois = await estado(client, id_variacao)
    if depois != esperado:
        logger.error("balança do produto %s não confere depois de gravar: esperado %s, lido %s", id_variacao, esperado, depois)
        raise BalancaRecusada("O ERP respondeu, mas a balança não ficou como pedido. Reabra a ficha e confira no ERP.", 502)
    alteracao = _registrar(cnpj, email, nome, id_variacao, "alteracao", antes, novo)
    logger.info("balança alterada: empresa %s por %s, produto %s: %s -> %s", cnpj, email, id_variacao, antes, novo)
    return {"alteracao": alteracao, "exportar": novo["exportar"], "solicitar": novo["solicitar"], "unidade": antes["unidade"]}


async def desfazer(client: MeuERPClient, cnpj: str, email: str, nome: str, id_variacao: int, id_alteracao: str) -> dict:
    with db_session() as conn:
        linha = conn.execute(
            "SELECT tipo, exportar_antes, solicitar_antes, exportar_novo, solicitar_novo, criado_em, desfeita_em, id_variacao"
            " FROM alteracoes_balanca WHERE id = ? AND cnpj = ?", (id_alteracao, cnpj),
        ).fetchone()
    if not linha or linha["tipo"] != "alteracao" or int(linha["id_variacao"]) != int(id_variacao):
        raise BalancaRecusada("Alteração não encontrada.", 404)
    if linha["desfeita_em"]:
        raise BalancaRecusada("Esta alteração já foi desfeita.", 409)
    criada = datetime.strptime(linha["criado_em"], FORMATO_HORA).replace(tzinfo=timezone.utc)
    if _agora() - criada > JANELA_DESFAZER:
        raise BalancaRecusada("O prazo para desfazer (30 minutos) passou. Faça uma nova alteração.", 409)
    deixou = {"exportar": bool(linha["exportar_novo"]), "solicitar": bool(linha["solicitar_novo"])}
    voltar = {"exportar": bool(linha["exportar_antes"]), "solicitar": bool(linha["solicitar_antes"])}
    atual = await estado(client, id_variacao)
    if {"exportar": atual["exportar"], "solicitar": atual["solicitar"]} != deixou:
        raise BalancaRecusada("A balança mudou depois desta alteração, então não dá para desfazer automaticamente. Faça uma nova alteração.", 409)

    # Reserva o desfazer antes de gravar: dois toques seguidos não desfazem duas vezes.
    with db_session() as conn:
        reservado = conn.execute("UPDATE alteracoes_balanca SET desfeita_em = ? WHERE id = ? AND cnpj = ? AND desfeita_em IS NULL",
                                 (_agora().strftime(FORMATO_HORA), id_alteracao, cnpj)).rowcount
    if not reservado:
        raise BalancaRecusada("Esta alteração já foi desfeita.", 409)
    try:
        await _gravar(client, id_variacao, _campos(deixou, voltar), {**voltar, "unidade": atual["unidade"]})
        depois = await estado(client, id_variacao)
        if {"exportar": depois["exportar"], "solicitar": depois["solicitar"]} != voltar:
            raise BalancaRecusada("O ERP respondeu, mas a balança não voltou como pedido. Reabra a ficha e confira no ERP.", 502)
    except Exception:
        with db_session() as conn:
            conn.execute("UPDATE alteracoes_balanca SET desfeita_em = NULL WHERE id = ? AND cnpj = ?", (id_alteracao, cnpj))
        raise
    _registrar(cnpj, email, nome, id_variacao, "desfazer", deixou, voltar)
    logger.info("alteração de balança desfeita: empresa %s por %s, produto %s: %s -> %s", cnpj, email, id_variacao, deixou, voltar)
    return {"exportar": voltar["exportar"], "solicitar": voltar["solicitar"], "unidade": atual["unidade"]}
