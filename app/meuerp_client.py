"""Async client for the "Meu ERP Online" public API.

Rate limit is per API token (per docs): 20 requests burst, refilling at
20/minute, with a 10-request server-side queue before HTTP 429 kicks in.
We enforce the same budget client-side with a token bucket so we stay
under the limit instead of reacting to 429s after the fact.
"""

import asyncio
import time

import httpx

from app.config import settings

RATE_LIMIT_CAPACITY = 20
RATE_LIMIT_REFILL_SECONDS = 60
MAX_RETRIES = 3


class _TokenBucket:
    def __init__(self, capacity: int, refill_seconds: int):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = capacity / refill_seconds
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.last_refill = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.refill_rate)


_buckets: dict[str, _TokenBucket] = {}
_shared_clients: dict[str, httpx.AsyncClient] = {}


def _bucket_for(token: str) -> _TokenBucket:
    if token not in _buckets:
        _buckets[token] = _TokenBucket(RATE_LIMIT_CAPACITY, RATE_LIMIT_REFILL_SECONDS)
    return _buckets[token]


def _client_for(base_url: str) -> httpx.AsyncClient:
    # Reaberto por request, cada chamada pagava um novo handshake TCP/TLS (~1s+),
    # o que tornava uma busca com 8 produtos (~17 chamadas) lenta o bastante para
    # travar a UI. Um client HTTP compartilhado reaproveita a conexão (keep-alive).
    if base_url not in _shared_clients:
        _shared_clients[base_url] = httpx.AsyncClient(base_url=base_url, timeout=30.0)
    return _shared_clients[base_url]


class MeuERPClient:
    def __init__(self, api_token: str, base_url: str | None = None):
        self.api_token = api_token
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
            await self.bucket.acquire()
            response = await client.request(method, path, headers=headers, **kwargs)
            if response.status_code == 429 and attempt < MAX_RETRIES:
                retry_after = float(response.headers.get("Retry-After", 5))
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")
