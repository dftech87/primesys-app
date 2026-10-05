"""Cache em memória, compartilhado entre todos os usuários de uma mesma empresa.

O ERP deixa só 20 chamadas por minuto por cliente, então o que não muda a cada segundo (lista de lojas,
contas a pagar, estoque mínimo...) não deve ser buscado de novo a cada toque. Este módulo cuida de:

  - validade (TTL) por consulta;
  - chamadas simultâneas iguais viram uma só (duas pessoas, ou um toque duplo, não gastam o dobro);
  - "obsoleto_ate": depois de vencido, ainda entrega o valor antigo na hora e renova por trás;
  - descarta primeiro o que foi usado há mais tempo (LRU), em vez de zerar tudo quando enche.

A chave SEMPRE deve começar pelo CNPJ do cliente: é isso que impede que o resultado de uma empresa
seja entregue para outra. Vale para um único processo (o limite de chamadas também é por processo).
"""

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_MAX_ENTRADAS = 1000
_guardado: "OrderedDict[tuple, tuple[float, object]]" = OrderedDict()
_em_andamento: dict[tuple, asyncio.Task] = {}


async def cached(
    chave: tuple,
    ttl_segundos: float,
    produzir: Callable[[], Awaitable],
    guardar_se: Callable[[object], bool] | None = None,
    obsoleto_ate: float = 0,
):
    """`guardar_se` permite não cachear um resultado incompleto (ex.: uma seção que falhou).

    `obsoleto_ate`: por quantos segundos depois de vencido o valor antigo ainda pode ser entregue
    na hora enquanto uma renovação roda em segundo plano.
    """
    agora = time.monotonic()
    acerto = _guardado.get(chave)
    if acerto:
        expira, valor = acerto
        if expira > agora:
            _guardado.move_to_end(chave)
            return valor
        if obsoleto_ate and expira + obsoleto_ate > agora:
            _renovar(chave, ttl_segundos, produzir, guardar_se).add_done_callback(_ignorar_erro)
            return valor
    return await asyncio.shield(_renovar(chave, ttl_segundos, produzir, guardar_se))


def _renovar(chave, ttl_segundos, produzir, guardar_se) -> asyncio.Task:
    """Uma única renovação por chave de cada vez; quem chegar enquanto ela roda aproveita o resultado."""
    tarefa = _em_andamento.get(chave)
    if tarefa is None:
        tarefa = asyncio.create_task(_calcular(chave, ttl_segundos, produzir, guardar_se))
        _em_andamento[chave] = tarefa
        tarefa.add_done_callback(lambda _t: _em_andamento.pop(chave, None))
    return tarefa


async def _calcular(chave, ttl_segundos, produzir, guardar_se):
    valor = await produzir()
    if guardar_se is None or guardar_se(valor):
        _guardado[chave] = (time.monotonic() + ttl_segundos, valor)
        _guardado.move_to_end(chave)
        while len(_guardado) > _MAX_ENTRADAS:
            _guardado.popitem(last=False)
    return valor


def _ignorar_erro(tarefa: asyncio.Task) -> None:
    """Renovação em segundo plano que falhou: o valor antigo continua valendo, só registramos."""
    if not tarefa.cancelled() and tarefa.exception() is not None:
        logger.warning("renovação em segundo plano falhou: %r", tarefa.exception())
