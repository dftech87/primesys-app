"""Rotas da liberação de permissão do caixa no Prime Info (regras em app/liberacoes.py)."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import liberacoes, push, versao
from app.deps import current_tenant, revalidar_acessos, sem_permissao
from app.meuerp_client import MeuERPClient
from app.routers.preco import _mesma_origem, _quem

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(revalidar_acessos)])
templates = Jinja2Templates(directory="app/templates")


class PedidoDecisao(BaseModel):
    tipo: str = Field(max_length=1)          # L = liberar, B = bloquear
    motivo: str = Field(default="", max_length=256)


class ChavesPush(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class PedidoInscricao(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: ChavesPush


class PedidoCancelar(BaseModel):
    endpoint: str = Field(max_length=1000)


def _entrar(request: Request) -> tuple[dict | None, JSONResponse | None]:
    tenant = current_tenant(request)
    if tenant is None:
        return None, JSONResponse({"detail": "not authenticated"}, status_code=401)
    if not liberacoes.pode(request):
        return None, sem_permissao()
    return tenant, None


@router.get("/liberacoes", response_class=HTMLResponse)
async def pagina(request: Request):
    tenant = current_tenant(request)
    if tenant is None:
        return RedirectResponse("/login?next=/liberacoes", status_code=303)
    if not liberacoes.pode(request):
        return RedirectResponse("/produtos", status_code=303)
    return templates.TemplateResponse(
        request, "liberacoes.html", {"empresa_nome": tenant["nome_fantasia"], "versao": versao.VERSAO},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/liberacoes")
async def listar(request: Request):
    """Pedidos pendentes. Lê o que o servidor já guardou (sem chamar o ERP); só na primeira abertura faz uma leitura."""
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    cnpj = tenant["cnpj"]
    liberacoes.marcar_presenca(cnpj)
    await liberacoes.garantir_fresco(cnpj, tenant)
    return {**liberacoes.resumo(cnpj), "push": {"disponivel": push.ligado(), "chave": push.chave_publica() if push.ligado() else None}}


@router.post("/api/liberacoes/{id_solicitacao}")
async def decidir(request: Request, id_solicitacao: int, pedido: PedidoDecisao):
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    if not _mesma_origem(request):
        return JSONResponse({"detail": "Pedido de outro site recusado."}, status_code=403)
    email, nome = _quem(request, tenant)
    try:
        resultado = await liberacoes.decidir(MeuERPClient(tenant["api_token"]), tenant["cnpj"], email, nome, id_solicitacao, pedido.tipo, pedido.motivo)
    except liberacoes.LiberacaoRecusada as erro:
        return JSONResponse({"detail": erro.detalhe}, status_code=erro.status)
    liberacoes.marcar_presenca(tenant["cnpj"])
    return {**resultado, **{k: v for k, v in liberacoes.resumo(tenant["cnpj"]).items() if k == "itens"}}


@router.post("/api/push/inscrever")
async def inscrever(request: Request, pedido: PedidoInscricao):
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    if not _mesma_origem(request):
        return JSONResponse({"detail": "Pedido de outro site recusado."}, status_code=403)
    if not push.ligado():
        return JSONResponse({"detail": "As notificações ainda não estão ligadas neste servidor."}, status_code=503)
    if not liberacoes.endereco_de_push_valido(pedido.endpoint):
        return JSONResponse({"detail": "Endereço de notificação não reconhecido."}, status_code=422)
    liberacoes.inscrever(tenant["cnpj"], request.session["email"], int(request.session["perfil"]), pedido.endpoint, pedido.keys.p256dh, pedido.keys.auth)
    return {"ok": True}


@router.post("/api/push/cancelar")
async def cancelar(request: Request, pedido: PedidoCancelar):
    tenant, recusa = _entrar(request)
    if recusa:
        return recusa
    if not _mesma_origem(request):
        return JSONResponse({"detail": "Pedido de outro site recusado."}, status_code=403)
    liberacoes.remover_inscricao(pedido.endpoint, tenant["cnpj"])
    return {"ok": True}
