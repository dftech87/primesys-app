"""Alterar o preço de venda pelo Prime Info: a única escrita do app no ERP, por isso com várias travas.

  - quem pode: permissão do perfil no ERP (acessos.pode_alterar_preco), conferida nas rotas;
  - o preço é lido do ERP na hora (nunca do cache) e a alteração é recusada se mudou desde que a pessoa abriu a ficha;
  - mudança acima de 30%, ou preço abaixo do custo, só passa com confirmação explícita (quem não vê custo nem passa);
  - tudo fica registrado aqui (o ERP grava como "API-Publica"): quem, quando, de quanto para quanto;
  - dá para desfazer por 30 minutos, desde que o preço ainda seja o que a alteração deixou.
"""

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx

from app.cache import descartar, descartar_prefixo
from app.compras import nome_publico
from app.datas import FUSO_LOJA
from app.database import db_session
from app.meuerp_client import MeuERPClient

logger = logging.getLogger(__name__)

PRECO_MAXIMO = 999_999.99
VARIACAO_GRANDE_PCT = 30.0
JANELA_DESFAZER = timedelta(minutes=30)
FORMATO_HORA = "%Y-%m-%d %H:%M:%S.%f"   # UTC; com frações de segundo para a ordem exata (alterar e desfazer em seguida)
TOLERANCIA = 0.005


class PrecoRecusado(Exception):
    """Motivo claro para a pessoa (vai direto para a tela); `extra` leva dados para a tela reagir."""

    def __init__(self, detalhe: str, status: int = 422, **extra):
        super().__init__(detalhe)
        self.detalhe, self.status, self.extra = detalhe, status, extra


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _centavos(valor) -> float:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        raise PrecoRecusado("Informe um preço válido.")
    if not math.isfinite(numero):
        raise PrecoRecusado("Informe um preço válido.")
    arredondado = float(Decimal(str(numero)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if arredondado < 0.01:
        raise PrecoRecusado("O preço precisa ser de pelo menos R$ 0,01.")
    if arredondado > PRECO_MAXIMO:
        raise PrecoRecusado("Esse preço é alto demais. Confira o valor digitado.")
    return arredondado


async def tabelas_ativas(client: MeuERPClient, id_variacao: int) -> list[dict]:
    """Tabelas de preço ativas do produto, lidas do ERP agora (a primeira é a que a ficha mostra)."""
    resultado = await client.get(f"/api/mercadoria/{int(id_variacao)}/tabelas-preco/v1", params={"limit": 10})
    itens = [i for i in (resultado.get("items") or []) if i.get("ativo") == "S"]
    itens.sort(key=lambda i: i.get("codigoTabela") or 0)
    return [{"codigo": int(i["codigoTabela"]), "preco": float(i.get("valPreco") or 0), "vigencia": i.get("validarVigencia") == "S"}
            for i in itens]


async def custo_atual(client: MeuERPClient, id_variacao: int) -> float | None:
    try:
        return (await client.get(f"/api/mercadoria/{int(id_variacao)}/custo/v1")).get("valCusto")
    except httpx.HTTPError:
        return None


def _escolher(tabelas: list[dict], codigo: int | None) -> dict:
    if not tabelas:
        raise PrecoRecusado("Este produto não tem tabela de preço ativa. Cadastre o preço no ERP.")
    if codigo is None:
        escolhida = tabelas[0]
    else:
        escolhida = next((t for t in tabelas if t["codigo"] == int(codigo)), None)
        if escolhida is None:
            raise PrecoRecusado("Tabela de preço não encontrada para este produto.")
    if escolhida["vigencia"]:
        raise PrecoRecusado("Esta tabela de preço tem vigência programada. Altere o preço direto no ERP.")
    return escolhida


def avaliar(atual: float, novo: float, custo, pode_ver_custo: bool, confirmo: bool) -> list[str]:
    """Motivos que pedem confirmação. Recusa de vez o que o perfil não pode (abaixo do custo sem ver custo)."""
    motivos = []
    if atual > 0 and abs(novo - atual) / atual * 100 > VARIACAO_GRANDE_PCT:
        motivos.append("variacao")
    if custo and custo > 0 and novo < float(custo) - TOLERANCIA:
        if not pode_ver_custo:
            raise PrecoRecusado("Esse preço ficaria abaixo do custo, e o seu perfil não permite vender abaixo do custo.", 403)
        motivos.append("abaixo_custo")
    if motivos and not confirmo:
        raise PrecoRecusado("Confirme a alteração para continuar.", 409, confirmar=motivos)
    return motivos


async def _gravar(client: MeuERPClient, id_variacao: int, tabela: int, preco: float) -> dict:
    corpo = {"idMercadoriaVariacao": int(id_variacao), "idTabela": int(tabela), "preco": preco}
    try:
        resposta = await client.put("/api/mercadoria-tabela-preco/v1", json=corpo)
    except httpx.HTTPStatusError as erro:
        codigo = erro.response.status_code
        if codigo in (401, 403):
            raise PrecoRecusado("O ERP não liberou a alteração de preço para esta empresa (permissão do acesso do aplicativo).", 403)
        mensagem = ""
        try:
            mensagem = str(erro.response.json().get("mensagem") or "")[:200]
        except Exception:
            pass
        logger.warning("ERP recusou a alteração de preço (HTTP %s): %s", codigo, mensagem or erro.response.text[:200])
        raise PrecoRecusado(mensagem or "O ERP não aceitou a alteração. Tente de novo ou altere direto no ERP.", 502 if codigo >= 500 else 422)
    except httpx.HTTPError:
        # Sem resposta: pode ter gravado mesmo assim. Confere no ERP antes de dizer que falhou.
        try:
            depois = await tabelas_ativas(client, id_variacao)
            if any(t["codigo"] == int(tabela) and abs(t["preco"] - preco) < TOLERANCIA for t in depois):
                logger.warning("o ERP demorou, mas o preço do produto %s foi gravado", id_variacao)
                return {}
        except httpx.HTTPError:
            pass
        raise PrecoRecusado("Não foi possível confirmar se o ERP gravou o novo preço. Reabra a ficha e confira antes de tentar de novo.", 502)
    if isinstance(resposta, dict) and resposta.get("status") is False:
        raise PrecoRecusado(str(resposta.get("mensagem") or "O ERP não aceitou a alteração.")[:200])
    return resposta if isinstance(resposta, dict) else {}


def _registrar(cnpj: str, email: str, nome: str, id_variacao: int, tabela: int, tipo: str, anterior: float, novo: float) -> str | None:
    chave = uuid.uuid4().hex
    try:
        with db_session() as conn:
            conn.execute(
                "INSERT INTO alteracoes_preco (id, cnpj, email, nome, id_variacao, tabela, tipo, preco_anterior, preco_novo, criado_em)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chave, cnpj, email, nome, int(id_variacao), int(tabela), tipo, anterior, novo, _agora().strftime(FORMATO_HORA)),
            )
    except Exception:
        # O preço já foi gravado no ERP: não dá para desfazer isso aqui. Fica o alerta nos logs.
        logger.exception("ALTERAÇÃO DE PREÇO SEM REGISTRO: %s %s produto %s tabela %s %s -> %s", cnpj, email, id_variacao, tabela, anterior, novo)
        return None
    return chave


def _esquecer(cnpj: str, id_variacao: int) -> None:
    """Tudo que guardava o preço antigo deste produto sai do cache."""
    descartar((cnpj, "preco", id_variacao))
    descartar((cnpj, "historico-preco", id_variacao))
    descartar_prefixo((cnpj, "precos-lista"))


async def alterar(client: MeuERPClient, cnpj: str, email: str, nome: str, id_variacao: int, preco, preco_visto, tabela: int | None,
                  confirmo: bool, pode_ver_custo: bool) -> dict:
    novo = _centavos(preco)
    escolhida = _escolher(await tabelas_ativas(client, id_variacao), tabela)
    atual = escolhida["preco"]
    if preco_visto is not None and abs(float(preco_visto) - atual) > TOLERANCIA:
        raise PrecoRecusado("O preço mudou no ERP depois que você abriu esta ficha. Confira o valor atual e tente de novo.", 409, mudou=True, atual=atual)
    if abs(novo - atual) < TOLERANCIA:
        raise PrecoRecusado("O novo preço é igual ao preço atual.")
    avaliar(atual, novo, await custo_atual(client, id_variacao), pode_ver_custo, confirmo)

    resposta = await _gravar(client, id_variacao, escolhida["codigo"], novo)
    try:
        anterior = float(resposta.get("precoAnterior", atual))
    except (TypeError, ValueError):
        anterior = atual
    alteracao = _registrar(cnpj, email, nome, id_variacao, escolhida["codigo"], "alteracao", anterior, novo)
    _esquecer(cnpj, id_variacao)
    logger.info("preço alterado: empresa %s por %s, produto %s tabela %s: %s -> %s", cnpj, email, id_variacao, escolhida["codigo"], anterior, novo)
    return {"alteracao": alteracao, "precoAnterior": anterior, "precoNovo": novo, "tabela": escolhida["codigo"]}


async def desfazer(client: MeuERPClient, cnpj: str, email: str, nome: str, id_variacao: int, id_alteracao: str) -> dict:
    with db_session() as conn:
        linha = conn.execute(
            "SELECT tipo, tabela, preco_anterior, preco_novo, criado_em, desfeita_em, id_variacao FROM alteracoes_preco WHERE id = ? AND cnpj = ?",
            (id_alteracao, cnpj),
        ).fetchone()
    if not linha or linha["tipo"] != "alteracao" or int(linha["id_variacao"]) != int(id_variacao):
        raise PrecoRecusado("Alteração não encontrada.", 404)
    if linha["desfeita_em"]:
        raise PrecoRecusado("Esta alteração já foi desfeita.", 409)
    criada = datetime.strptime(linha["criado_em"], FORMATO_HORA).replace(tzinfo=timezone.utc)
    if _agora() - criada > JANELA_DESFAZER:
        raise PrecoRecusado("O prazo para desfazer (30 minutos) passou. Faça uma nova alteração de preço.", 409)
    tabelas = await tabelas_ativas(client, id_variacao)
    escolhida = next((t for t in tabelas if t["codigo"] == int(linha["tabela"])), None)
    if escolhida is None or abs(escolhida["preco"] - float(linha["preco_novo"])) > TOLERANCIA:
        raise PrecoRecusado("O preço mudou depois desta alteração, então não dá para desfazer automaticamente. Faça uma nova alteração.", 409)

    # Reserva o desfazer antes de gravar: dois toques seguidos não desfazem duas vezes.
    with db_session() as conn:
        reservado = conn.execute(
            "UPDATE alteracoes_preco SET desfeita_em = ? WHERE id = ? AND cnpj = ? AND desfeita_em IS NULL",
            (_agora().strftime(FORMATO_HORA), id_alteracao, cnpj),
        ).rowcount
    if not reservado:
        raise PrecoRecusado("Esta alteração já foi desfeita.", 409)
    try:
        await _gravar(client, id_variacao, int(linha["tabela"]), float(linha["preco_anterior"]))
    except Exception:
        with db_session() as conn:
            conn.execute("UPDATE alteracoes_preco SET desfeita_em = NULL WHERE id = ? AND cnpj = ?", (id_alteracao, cnpj))
        raise
    _registrar(cnpj, email, nome, id_variacao, int(linha["tabela"]), "desfazer", float(linha["preco_novo"]), float(linha["preco_anterior"]))
    _esquecer(cnpj, id_variacao)
    logger.info("alteração de preço desfeita: empresa %s por %s, produto %s: %s -> %s", cnpj, email, id_variacao, linha["preco_novo"], linha["preco_anterior"])
    return {"precoNovo": float(linha["preco_anterior"]), "tabela": int(linha["tabela"])}


def _quem(linha) -> str:
    """Nome de quem alterou (o do ERP); sem nome, a parte do e-mail antes do @. O e-mail inteiro nunca sai do servidor."""
    return (linha["nome"] or "").strip() or str(linha["email"]).split("@")[0]


def rotular_historico(cnpj: str, id_variacao: int, mudancas: list[dict]) -> list[dict]:
    """O ERP grava as alterações feitas pela API como "API-Publica" (aparece "Api"). As que foram feitas por aqui
    saem como "APP - Nome": confere preço de e para e o dia com o registro do app (cada registro vale uma vez)."""
    if not any(m.get("quem") == "Api" for m in mudancas):
        return mudancas
    with db_session() as conn:
        linhas = conn.execute(
            "SELECT email, nome, preco_anterior, preco_novo, criado_em FROM alteracoes_preco"
            " WHERE cnpj = ? AND id_variacao = ? ORDER BY criado_em DESC, id DESC LIMIT 100",
            (cnpj, int(id_variacao)),
        ).fetchall()
    livres = [{"de": float(l["preco_anterior"]), "para": float(l["preco_novo"]), "quem": _quem(l),
               "dia": datetime.strptime(l["criado_em"], FORMATO_HORA).replace(tzinfo=timezone.utc).astimezone(FUSO_LOJA).date()} for l in linhas]
    saida = []
    for m in mudancas:
        if m.get("quem") == "Api":
            try:
                dia = datetime.strptime(m["data"], "%d/%m/%Y").date()
            except (KeyError, ValueError):
                dia = None
            achada = next((k for k, l in enumerate(livres)
                           if dia and abs((l["dia"] - dia).days) <= 1 and abs(l["de"] - m["de"]) < TOLERANCIA and abs(l["para"] - m["para"]) < TOLERANCIA), None)
            if achada is not None:
                m = {**m, "quem": f"APP - {nome_publico(livres.pop(achada)['quem'])}"}
        saida.append(m)
    return saida


def recentes(cnpj: str, id_variacao: int, limite: int = 8) -> list[dict]:
    with db_session() as conn:
        linhas = conn.execute(
            "SELECT email, nome, tipo, preco_anterior, preco_novo, criado_em, desfeita_em FROM alteracoes_preco"
            " WHERE cnpj = ? AND id_variacao = ? ORDER BY criado_em DESC, id DESC LIMIT ?",
            (cnpj, int(id_variacao), int(limite)),
        ).fetchall()
    return [{"quando": f"{l['criado_em'].replace(' ', 'T')}Z", "quem": _quem(l), "tipo": l["tipo"],
             "de": float(l["preco_anterior"]), "para": float(l["preco_novo"]), "desfeita": bool(l["desfeita_em"])} for l in linhas]
