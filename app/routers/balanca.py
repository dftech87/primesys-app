"""Rotas de Exportar/Solicitar balança na ficha do produto (regras em app/balanca.py)."""

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import acessos, balanca
from app.deps import current_tenant, revalidar_acessos, sem_permissao
from app.meuerp_client import MeuERPClient
from app.routers.preco import _erro_erp, _mesma_origem, _quem

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(revalidar_acessos)])   # acessos reconferidos a cada 5 min com o app aberto


class PedidoBalanca(BaseModel):
    exportar: bool
    solicitar: bool
    confirmo: bool = False


class PedidoDesfazerBalanca(BaseModel):
    alteracao: str


def _entrar(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Quem o perfil libera (permissão 3901 + aba Produtos) numa empresa com a balança liberada no /admin. A sessão precisa saber o e-mail: é quem fica no registro."""
    tenant = current_tenant(request)
    if tenant is None:
        return None, JSONResponse({"detail": "not authenticated"}, status_code=401)
    if not (acessos.pode(request, "balanca") and acessos.pode(request, "produtos") and request.session.get("email")):
        return None, sem_permissao()
    if not tenant.get("balanca_liberada"):   # liberação por empresa (botão no /admin): desligada até o dono ligar
        return None, sem_permissao()
    return tenant, None


@router.get("/api/produtos/{id_variacao}/balanca")
async def ler_balanca(request: Request, id_variacao: int):
    """O que a ficha mostra: Exportar e Solicitar atuais (lidos do ERP agora) e a unidade do produto."""
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    try:
        return await balanca.estado(MeuERPClient(tenant["api_token"]), id_variacao)
    except balanca.BalancaRecusada as erro:
        return JSONResponse({"detail": erro.detalhe, **erro.extra}, status_code=erro.status)
    except httpx.HTTPError as erro:
        return _erro_erp(erro)


@router.post("/api/produtos/{id_variacao}/balanca")
async def alterar_balanca(request: Request, id_variacao: int, pedido: PedidoBalanca):
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    if not _mesma_origem(request):
        return JSONResponse({"detail": "Pedido de outro site recusado."}, status_code=403)
    try:
        return await balanca.alterar(MeuERPClient(tenant["api_token"]), tenant["cnpj"], *_quem(request, tenant), id_variacao,
                                     pedido.exportar, pedido.solicitar, pedido.confirmo)
    except balanca.BalancaRecusada as erro:
        return JSONResponse({"detail": erro.detalhe, **erro.extra}, status_code=erro.status)
    except httpx.HTTPError as erro:
        return _erro_erp(erro)


@router.post("/api/produtos/{id_variacao}/balanca/desfazer")
async def desfazer_balanca(request: Request, id_variacao: int, pedido: PedidoDesfazerBalanca):
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    if not _mesma_origem(request):
        return JSONResponse({"detail": "Pedido de outro site recusado."}, status_code=403)
    try:
        return await balanca.desfazer(MeuERPClient(tenant["api_token"]), tenant["cnpj"], *_quem(request, tenant), id_variacao, pedido.alteracao)
    except balanca.BalancaRecusada as erro:
        return JSONResponse({"detail": erro.detalhe, **erro.extra}, status_code=erro.status)
    except httpx.HTTPError as erro:
        return _erro_erp(erro)
