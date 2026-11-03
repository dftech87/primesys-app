"""Rotas de alteração do preço de venda na ficha do produto (regras em app/preco_venda.py)."""

import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import acessos, preco_venda
from app.deps import current_tenant, sem_permissao
from app.meuerp_client import MeuERPClient
from app.tenants import ERRO_ERP_LENTO

logger = logging.getLogger(__name__)
router = APIRouter()


class PedidoPreco(BaseModel):
    preco: float
    preco_visto: float | None = None   # o preço que a pessoa estava vendo na ficha (barra se mudou no ERP)
    tabela: int | None = None
    confirmo: bool = False


class PedidoDesfazer(BaseModel):
    alteracao: str


def _entrar(request: Request) -> tuple[dict | None, JSONResponse | None]:
    tenant = current_tenant(request)
    if tenant is None:
        return None, JSONResponse({"detail": "not authenticated"}, status_code=401)
    if not acessos.pode_alterar_preco(request):
        return None, sem_permissao()
    return tenant, None


def _quem(request: Request, tenant: dict) -> tuple[str, str]:
    email = request.session["email"]
    nome = (request.session.get("nome") or acessos.nome_do_email(tenant["cnpj"], email) or "").strip()
    return email, nome or email.split("@")[0]


def _mesma_origem(request: Request) -> bool:
    """Escrita só do próprio site (o cookie já é SameSite=lax; isto é uma segunda trava). Sem cabeçalho Origin, vale."""
    origem = request.headers.get("origin")
    if not origem:
        return True
    destino = urlparse(origem).netloc
    return destino in {request.headers.get("host"), request.headers.get("x-forwarded-host")}


def _erro_erp(erro: httpx.HTTPError) -> JSONResponse:
    if isinstance(erro, httpx.TimeoutException):
        return JSONResponse({"detail": ERRO_ERP_LENTO}, status_code=504)
    return JSONResponse({"detail": "Não foi possível falar com o ERP agora. Tente de novo em instantes."}, status_code=502)


@router.get("/api/produtos/{id_variacao}/edicao-preco")
async def dados_da_edicao(request: Request, id_variacao: int):
    """O que a folha de edição precisa: preço atual de cada tabela (lido do ERP agora) e, para quem vê custo, o custo."""
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    client = MeuERPClient(tenant["api_token"])
    ver_custo = acessos.pode(request, "custo")
    try:
        tabelas, custo = await preco_venda.estado(client, id_variacao)   # preço e custo lidos agora, numa chamada só
        if not ver_custo:
            custo = None   # o custo só vai para quem o perfil deixa ver
    except httpx.HTTPError as erro:
        return _erro_erp(erro)
    return {"tabelas": tabelas, "custo": custo, "limiteVariacaoPct": preco_venda.VARIACAO_GRANDE_PCT}


@router.post("/api/produtos/{id_variacao}/preco")
async def alterar_preco(request: Request, id_variacao: int, pedido: PedidoPreco):
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    if not _mesma_origem(request):
        return JSONResponse({"detail": "Pedido de outro site recusado."}, status_code=403)
    client = MeuERPClient(tenant["api_token"])
    try:
        return await preco_venda.alterar(
            client, tenant["cnpj"], *_quem(request, tenant), id_variacao, pedido.preco, pedido.preco_visto, pedido.tabela,
            pedido.confirmo, acessos.pode(request, "custo"),
        )
    except preco_venda.PrecoRecusado as erro:
        return JSONResponse({"detail": erro.detalhe, **erro.extra}, status_code=erro.status)
    except httpx.HTTPError as erro:
        return _erro_erp(erro)


@router.post("/api/produtos/{id_variacao}/preco/desfazer")
async def desfazer_preco(request: Request, id_variacao: int, pedido: PedidoDesfazer):
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    if not _mesma_origem(request):
        return JSONResponse({"detail": "Pedido de outro site recusado."}, status_code=403)
    client = MeuERPClient(tenant["api_token"])
    try:
        return await preco_venda.desfazer(client, tenant["cnpj"], *_quem(request, tenant), id_variacao, pedido.alteracao)
    except preco_venda.PrecoRecusado as erro:
        return JSONResponse({"detail": erro.detalhe, **erro.extra}, status_code=erro.status)
    except httpx.HTTPError as erro:
        return _erro_erp(erro)


@router.get("/api/produtos/{id_variacao}/alteracoes-preco")
async def alteracoes_do_produto(request: Request, id_variacao: int):
    """Alterações de preço feitas pelo Prime Info neste produto (quem, quando, de quanto para quanto)."""
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    return {"alteracoes": preco_venda.recentes(tenant["cnpj"], id_variacao)}
