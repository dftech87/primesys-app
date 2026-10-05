"""Cache em memória com validade curta, para não repetir consultas pesadas.

A chave SEMPRE deve começar pelo CNPJ do cliente: é isso que impede que o
resultado de uma empresa seja entregue para outra.
"""

import time
from typing import Awaitable, Callable

_MAX_ENTRADAS = 512
_guardado: dict[tuple, tuple[float, object]] = {}


async def cached(
    chave: tuple,
    ttl_segundos: float,
    produzir: Callable[[], Awaitable],
    guardar_se: Callable[[object], bool] | None = None,
):
    """`guardar_se` permite não cachear um resultado incompleto (ex.: uma seção que falhou)."""
    agora = time.monotonic()
    acerto = _guardado.get(chave)
    if acerto and acerto[0] > agora:
        return acerto[1]

    valor = await produzir()
    if guardar_se is not None and not guardar_se(valor):
        return valor

    if len(_guardado) >= _MAX_ENTRADAS:
        for k in [k for k, (expira, _) in _guardado.items() if expira <= agora]:
            _guardado.pop(k, None)
        if len(_guardado) >= _MAX_ENTRADAS:
            _guardado.clear()
    _guardado[chave] = (agora + ttl_segundos, valor)
    return valor
