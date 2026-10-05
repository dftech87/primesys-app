"""Async client for the "Meu ERP Online" public API.

Rate limit is per API token (per docs): 20 requests burst, refilling at
20/minute, with a 10-request server-side queue before HTTP 429 kicks in.
We enforce the same budget client-side with a token bucket so we stay
under the limit instead of reacting to 429s after the fact.
"""

import asyncio
import hashlib
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RATE_LIMIT_CAPACITY = 20
RATE_LIMIT_REFILL_SECONDS = 60
MAX_RETRIES = 3
# Chamadas em segundo plano (ex.: o selo da aba Alertas) só usam a cota que passar desta reserva,
# para nunca atrasar o que a pessoa está esperando na tela (busca, dashboard...).
RESERVA_PARA_INTERATIVAS = 6
AVISAR_ESPERA_ACIMA_DE = 2.0


class _TokenBucket:
    def __init__(self, capacity: int, refill_seconds: int, nome: str = ""):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = capacity / refill_seconds
        self.last_refill = time.monotonic()
        self.nome = nome

    def _tentar(self, minimo: float) -> float:
        """Pega uma ficha se houver `minimo` disponíveis; senão devolve quanto falta esperar (s).
        Sem `await`, então é atômico dentro do loop do asyncio (não precisa de lock)."""
        agora = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (agora - self.last_refill) * self.refill_rate)
        self.last_refill = agora
        if self.tokens >= minimo:
            self.tokens -= 1
            return 0.0
        return max(0.05, (minimo - self.tokens) / self.refill_rate)

    async def acquire(self, segundo_plano: bool = False) -> None:
        minimo = 1 + (RESERVA_PARA_INTERATIVAS if segundo_plano else 0)
        inicio = time.monotonic()
        while True:
            espera = self._tentar(minimo)
            if espera == 0:
                break
            await asyncio.sleep(espera)
        esperou = time.monotonic() - inicio
        if esperou > AVISAR_ESPERA_ACIMA_DE:
            logger.warning("limite do ERP: cliente %s esperou %.1fs por cota", self.nome, esperou)


_buckets: dict[str, _TokenBucket] = {}
_shared_clients: dict[str, httpx.AsyncClient] = {}


def _bucket_for(token: str) -> _TokenBucket:
    if token not in _buckets:
        _buckets[token] = _TokenBucket(
            RATE_LIMIT_CAPACITY, RATE_LIMIT_REFILL_SECONDS, hashlib.sha1(token.encode()).hexdigest()[:6]
        )
    return _buckets[token]


def _client_for(base_url: str) -> httpx.AsyncClient:
    # Reaberto por request, cada chamada pagava um novo handshake TCP/TLS (~1s+),
    # o que tornava uma busca com 8 produtos (~17 chamadas) lenta o bastante para
    # travar a UI. Um client HTTP compartilhado reaproveita a conexão (keep-alive).
    if base_url not in _shared_clients:
        _shared_clients[base_url] = httpx.AsyncClient(base_url=base_url, timeout=30.0)
    return _shared_clients[base_url]


class MeuERPClient:
    def __init__(self, api_token: str, base_url: str | None = None, segundo_plano: bool = False):
        self.api_token = api_token
        self.segundo_plano = segundo_plano
        self.base_url = base_url or settings.meuerp_base_url
        self.bucket = _bucket_for(api_token)

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: dict | None = None) -> dict:
        return await self._request("POST", path, json=json)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Authentication {self.api_token}"}
        client = _client_for(self.base_url)
        for attempt in range(MAX_RETRIES + 1):
            await self.bucket.acquire(self.segundo_plano)
            response = await client.request(method, path, headers=headers, **kwargs)
            if response.status_code == 429 and attempt < MAX_RETRIES:
                retry_after = float(response.headers.get("Retry-After", 5))
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")
