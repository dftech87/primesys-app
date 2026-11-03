"""Medidor de consumo da cota do ERP: quantas chamadas cada tela gasta e quanto as pessoas esperaram na fila.

Só números, nada de dado de cliente: por empresa (identificada pelo mesmo código curto do balde de cota, que não revela o
token), por hora e por tela (o caminho com os números trocados por {n}, sem parâmetros). Fica em memória e é gravado a cada
minuto na tabela `consumo_erp` (sobrevive às publicações). Serve para decidir com dados o que otimizar: o que importa para o
usuário é a ESPERA (chamadas que passaram da cota), não só a contagem.

Só o Prime Info grava (`iniciar()` é chamado na subida dele); no Prime Vendas o registro fica desligado.
"""

import asyncio
import contextvars
import logging
import re
import time
from datetime import datetime, timedelta

from app.datas import FUSO_LOJA

logger = logging.getLogger(__name__)

tela: contextvars.ContextVar[str] = contextvars.ContextVar("tela_consumo", default="(sem tela)")
LIMITE_ESPERA_SENTIDA = 2.0   # acima disso a pessoa já nota a demora
INTERVALO_GRAVAR = 60
RETENCAO_DIAS = 30
MAX_CHAVES = 5000             # proteção: nunca deixar a memória crescer sem fim se a gravação travar

ativo = False
_contagem: dict[tuple[str, str, str], list[float]] = {}   # (cliente, hora, tela) -> [chamadas, esperas, espera_total, espera_max, segundo_plano]
_minutos: dict[tuple[str, str], int] = {}                 # (cliente, minuto) -> chamadas (para o pico por minuto)
_tarefa: asyncio.Task | None = None

_SEGMENTO_NUMERICO = re.compile(r"^\d+$")
_SEGMENTO_ID = re.compile(r"^[0-9a-f]{16,}$")


def normalizar(caminho: str) -> str:
    """'/api/produtos/22287/detalhe' -> '/api/produtos/{n}/detalhe'. Sem parâmetros de busca, sem códigos."""
    partes = []
    for parte in caminho.split("?", 1)[0].split("/"):
        partes.append("{n}" if _SEGMENTO_NUMERICO.match(parte) else "{id}" if _SEGMENTO_ID.match(parte) else parte[:40])
    return "/".join(partes)[:90] or "/"


def registrar(cliente: str, espera: float, segundo_plano: bool = False) -> None:
    """Uma chamada ao ERP (cada tentativa conta, porque cada uma gasta cota). Nunca levanta erro."""
    if not ativo:
        return
    try:
        agora = datetime.now(FUSO_LOJA)
        chave = (cliente, agora.strftime("%Y-%m-%d %H"), tela.get())
        if chave not in _contagem and len(_contagem) >= MAX_CHAVES:
            return
        linha = _contagem.setdefault(chave, [0, 0, 0.0, 0.0, 0])
        linha[0] += 1
        if espera > LIMITE_ESPERA_SENTIDA and not segundo_plano:
            linha[1] += 1
        linha[2] += espera
        linha[3] = max(linha[3], espera)
        linha[4] += 1 if segundo_plano else 0
        minuto = (cliente, agora.strftime("%Y-%m-%d %H:%M"))
        _minutos[minuto] = _minutos.get(minuto, 0) + 1
    except Exception:   # o medidor jamais pode atrapalhar uma chamada
        logger.debug("falha ao registrar consumo", exc_info=True)


_UPSERT = """
INSERT INTO consumo_erp (cliente, hora, tela, chamadas, esperas, espera_total, espera_max, segundo_plano, pico_minuto)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (cliente, hora, tela) DO UPDATE SET
    chamadas = consumo_erp.chamadas + excluded.chamadas,
    esperas = consumo_erp.esperas + excluded.esperas,
    espera_total = consumo_erp.espera_total + excluded.espera_total,
    espera_max = CASE WHEN excluded.espera_max > consumo_erp.espera_max THEN excluded.espera_max ELSE consumo_erp.espera_max END,
    segundo_plano = consumo_erp.segundo_plano + excluded.segundo_plano,
    pico_minuto = CASE WHEN excluded.pico_minuto > consumo_erp.pico_minuto THEN excluded.pico_minuto ELSE consumo_erp.pico_minuto END"""


def _gravar_agora() -> int:
    """Passa o que está em memória para a tabela. Devolve quantas linhas gravou."""
    from app.database import db_session   # importado aqui: o Prime Vendas importa o cliente do ERP e não precisa do banco

    global _contagem
    pendente, _contagem = _contagem, {}
    minuto_atual = datetime.now(FUSO_LOJA).strftime("%Y-%m-%d %H:%M")
    picos: dict[tuple[str, str], int] = {}
    for (cliente, minuto), n in list(_minutos.items()):
        hora = minuto[:13]
        picos[(cliente, hora)] = max(picos.get((cliente, hora), 0), n)
        if minuto < minuto_atual:
            _minutos.pop((cliente, minuto), None)
    if not pendente and not picos:
        return 0
    try:
        with db_session() as conn:
            for (cliente, hora, t), (chamadas, esperas, total, maximo, bg) in pendente.items():
                conn.execute(_UPSERT, (cliente, hora, t, int(chamadas), int(esperas), float(total), float(maximo), int(bg), 0))
            for (cliente, hora), pico in picos.items():   # linha "*" = a empresa inteira naquela hora
                conn.execute(_UPSERT, (cliente, hora, "*", 0, 0, 0.0, 0.0, 0, int(pico)))
    except Exception:
        logger.exception("não foi possível gravar o consumo do ERP; os números deste minuto foram perdidos")
        return 0
    return len(pendente) + len(picos)


def _limpar_antigos() -> None:
    from app.database import db_session

    limite = (datetime.now(FUSO_LOJA) - timedelta(days=RETENCAO_DIAS)).strftime("%Y-%m-%d %H")
    try:
        with db_session() as conn:
            conn.execute("DELETE FROM consumo_erp WHERE hora < ?", (limite,))
    except Exception:
        logger.exception("não foi possível limpar o consumo antigo")


async def _laco() -> None:
    ultimo_limpeza = 0.0
    while True:
        await asyncio.sleep(INTERVALO_GRAVAR)
        await asyncio.to_thread(_gravar_agora)
        if time.monotonic() - ultimo_limpeza > 24 * 3600:
            ultimo_limpeza = time.monotonic()
            await asyncio.to_thread(_limpar_antigos)


def iniciar() -> None:
    """Liga o medidor (chamado na subida do Prime Info, dentro do laço do servidor)."""
    global ativo, _tarefa
    ativo = True
    if _tarefa is None or _tarefa.done():
        _tarefa = asyncio.create_task(_laco())


async def parar() -> None:
    global ativo, _tarefa
    if _tarefa is not None:
        _tarefa.cancel()
        _tarefa = None
    await asyncio.to_thread(_gravar_agora)   # o que faltava gravar antes de desligar
    ativo = False


def resumo(dias: int = 1) -> dict:
    """Números para a tela do /admin: por empresa (código do balde) e por tela, nos últimos `dias` dias."""
    from app.database import db_session

    desde = (datetime.now(FUSO_LOJA) - timedelta(days=max(1, min(int(dias), RETENCAO_DIAS)))).strftime("%Y-%m-%d %H")
    with db_session() as conn:
        linhas = conn.execute(
            "SELECT cliente, hora, tela, chamadas, esperas, espera_total, espera_max, segundo_plano, pico_minuto"
            " FROM consumo_erp WHERE hora >= ?", (desde,)).fetchall()
    clientes: dict[str, dict] = {}
    for l in linhas:
        c = clientes.setdefault(l["cliente"], {"chamadas": 0, "esperas": 0, "espera_total": 0.0, "espera_max": 0.0, "pico_minuto": 0, "telas": {}})
        if l["tela"] == "*":
            c["pico_minuto"] = max(c["pico_minuto"], int(l["pico_minuto"]))
            continue
        c["chamadas"] += int(l["chamadas"]); c["esperas"] += int(l["esperas"]); c["espera_total"] += float(l["espera_total"])
        c["espera_max"] = max(c["espera_max"], float(l["espera_max"]))
        t = c["telas"].setdefault(l["tela"], {"chamadas": 0, "esperas": 0, "espera_max": 0.0, "segundo_plano": 0})
        t["chamadas"] += int(l["chamadas"]); t["esperas"] += int(l["esperas"]); t["segundo_plano"] += int(l["segundo_plano"])
        t["espera_max"] = max(t["espera_max"], float(l["espera_max"]))
    for c in clientes.values():
        c["telas"] = sorted(({"tela": k, **v} for k, v in c["telas"].items()), key=lambda x: (-x["esperas"], -x["chamadas"]))
    return clientes
