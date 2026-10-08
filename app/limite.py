"""Limite de tentativas de login (força bruta), em memória.

Janela deslizante de 15 minutos: depois de N falhas, a chave fica bloqueada até a falha mais antiga sair da
janela. Serve para um processo só (como o app roda hoje) e zera a cada deploy. Cada tentativa de login
também gasta uma das 20 chamadas por minuto do ERP daquele cliente, então bloquear cedo protege o app dele.

Chaves usadas: CNPJ+e-mail (principal), endereço de internet (rede toda) e endereço no /admin.
"""

import ipaddress
import time

from fastapi import Request

JANELA_SEGUNDOS = 15 * 60
LIMITE_POR_USUARIO = 5
LIMITE_POR_IP = 30      # generoso: vários funcionários podem sair pelo mesmo Wi-Fi
LIMITE_ADMIN = 5
_MAX_CHAVES = 20000

_falhas: dict[str, list[float]] = {}


def _recentes(chave: str, agora: float) -> list[float]:
    lista = [t for t in _falhas.get(chave, []) if agora - t < JANELA_SEGUNDOS]
    if lista:
        _falhas[chave] = lista
    else:
        _falhas.pop(chave, None)
    return lista


def segundos_bloqueado(chave: str, limite: int) -> int:
    """0 se pode tentar; senão, quantos segundos faltam para a chave ser liberada."""
    agora = time.monotonic()
    lista = _recentes(chave, agora)
    if len(lista) < limite:
        return 0
    return int(JANELA_SEGUNDOS - (agora - lista[len(lista) - limite])) + 1


def registrar_falha(chave: str) -> None:
    agora = time.monotonic()
    if len(_falhas) >= _MAX_CHAVES:   # evita crescer sem fim: descarta o que já saiu da janela
        for k in list(_falhas):
            _recentes(k, agora)
    _recentes(chave, agora)
    _falhas.setdefault(chave, []).append(agora)


def limpar(chave: str) -> None:
    _falhas.pop(chave, None)


def ip_do_cliente(request: Request) -> str | None:
    """Endereço de quem chamou. Atrás do proxy da hospedagem vale a ÚLTIMA entrada de X-Forwarded-For (a que o
    proxy acrescentou; as anteriores o cliente pode inventar). Endereço interno/privado não identifica ninguém:
    devolve None e o limite por rede é ignorado, para um erro de leitura nunca bloquear todo mundo junto."""
    bruto = request.headers.get("x-forwarded-for")
    candidato = bruto.split(",")[-1].strip() if bruto else (request.client.host if request.client else "")
    try:
        ip = ipaddress.ip_address(candidato)
    except ValueError:
        return None
    return str(ip) if ip.is_global else None


def mensagem_bloqueio(segundos: int) -> str:
    minutos = max(1, -(-segundos // 60))
    return f"Muitas tentativas. Tente novamente em {minutos} {'minuto' if minutos == 1 else 'minutos'}."
