"""Servidor do Prime Vendas (serviço próprio no Railway: uvicorn vendas.main:app).

Entrega o app do vendedor (PWA que funciona sem internet) e a API que ele usa. Reaproveita do PrimeSys Info só
as peças compartilhadas: cadastro das empresas (banco), criptografia dos tokens, cliente do ERP e limite de
tentativas de login. Não tem tela de administração: isso fica no /admin do PrimeSys Info.
"""

import hashlib
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from app import limite, meuerp_client
from app.config import COOKIE_SEGURO
from app.database import db_session
from app.security import normalize_cnpj
from app.tenants import authenticate, get_tenant_by_cnpj
from vendas import catalogo, config, sessao, vendedores
from vendas.db import criar_tabelas

logger = logging.getLogger(__name__)

# Fatia da cota do ERP que este processo pode usar (o PrimeSys Info usa a dele). Ajustado antes de qualquer chamada.
meuerp_client.RATE_LIMIT_CAPACITY = config.COTA_ERP_POR_MINUTO
meuerp_client.RESERVA_PARA_INTERATIVAS = 2

ESTATICOS = Path(__file__).parent / "static"


def _versao_dos_arquivos() -> str:
    """Muda sempre que um arquivo do app muda: o service worker usa para trocar o cache do celular."""
    resumo = hashlib.sha256()
    for arquivo in sorted(ESTATICOS.rglob("*")):
        if arquivo.is_file():
            resumo.update(str(arquivo.relative_to(ESTATICOS)).encode())
            resumo.update(arquivo.read_bytes())
    return resumo.hexdigest()[:12]


VERSAO = _versao_dos_arquivos()

app = FastAPI(title="Prime Vendas", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)

_CABECALHOS_SEGURANCA = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'",
}


@app.middleware("http")
async def cabecalhos(request: Request, call_next):
    resposta = await call_next(request)
    for nome, valor in _CABECALHOS_SEGURANCA.items():
        resposta.headers.setdefault(nome, valor)
    if COOKIE_SEGURO:   # na hospedagem (HTTPS)
        resposta.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/api/"):   # dados de uma empresa: nunca guardados por navegador ou intermediários
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


criar_tabelas()


@app.api_route("/saude", methods=["GET", "HEAD"])
async def saude():
    try:
        with db_session() as conn:
            conn.execute("SELECT 1 FROM pv_aparelhos LIMIT 1").fetchone()
    except Exception:
        return JSONResponse({"ok": False}, status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/versao")
async def versao():
    return {"versao": VERSAO}


class Entrada(BaseModel):
    cnpj: str = Field(max_length=32)
    email: str = Field(max_length=200)
    senha: str = Field(max_length=200)


def _erro(status: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"erro": mensagem}, status_code=status)


@app.post("/api/entrar")
async def entrar(dados: Entrada, request: Request):
    """Primeiro acesso do aparelho: valida e-mail e senha no ERP (nada é guardado), descobre o funcionário ligado
    ao usuário e devolve o token do aparelho + o que o app precisa saber (nome, comissão, permissões)."""
    cnpj, email = normalize_cnpj(dados.cnpj), dados.email.strip().lower()
    chave_usuario = f"vendas:{cnpj}:{email}"
    ip = limite.ip_do_cliente(request)
    chave_ip = f"vendas-ip:{ip}" if ip else None
    espera = max(limite.segundos_bloqueado(chave_usuario, limite.LIMITE_POR_USUARIO),
                 limite.segundos_bloqueado(chave_ip, limite.LIMITE_POR_IP) if chave_ip else 0)
    if espera:
        return _erro(429, limite.mensagem_bloqueio(espera))
    if not cnpj or not email or not dados.senha:
        return _erro(400, "Informe CNPJ, e-mail e senha.")

    def falhou():
        limite.registrar_falha(chave_usuario)
        if chave_ip:
            limite.registrar_falha(chave_ip)

    if not config.empresa_liberada(get_tenant_by_cnpj(cnpj)):   # confere no banco, antes de gastar chamada no ERP
        falhou()
        return _erro(403, "O Prime Vendas não está liberado para esta empresa. Fale com a DFTECH.")

    tenant, erro = await authenticate(cnpj, email, dados.senha)
    if erro:
        falhou()
        return _erro(401, erro)
    limite.limpar(chave_usuario)

    try:
        vendedor = await vendedores.do_email(tenant, email)
    except Exception:
        logger.exception("falha ao identificar o vendedor (empresa %s)", cnpj)
        return _erro(503, "Não foi possível consultar o ERP agora. Tente novamente em instantes.")
    if vendedor is None:
        return _erro(403, "Seu usuário do ERP não está ligado a um funcionário ativo. "
                          "No ERP, informe o seu usuário no cadastro de funcionário.")
    permissoes = await vendedores.permissoes(tenant, vendedor["id_perfil"])
    token = sessao.criar(cnpj, vendedor, aparelho=request.headers.get("user-agent", ""))
    logger.info("aparelho conectado: empresa %s, funcionário %s", cnpj, vendedor["id_funcionario"])
    return {
        "token": token,
        "empresa": {"cnpj": cnpj, "nome": tenant["nome_fantasia"]},
        "vendedor": {"id": vendedor["id_funcionario"], "nome": vendedor["nome"], "primeiro_nome": vendedor["primeiro_nome"],
                     "comissao": vendedor["per_comissao"]},
        "permissoes": permissoes,
        "pin_offline_dias": config.PIN_OFFLINE_DIAS,
    }


@app.get("/api/eu")
async def eu(aparelho: dict = Depends(sessao.aparelho_atual)):
    """O app chama ao abrir com internet: confirma que o acesso continua valendo."""
    return {"ok": True, "empresa": {"cnpj": aparelho["cnpj"], "nome": aparelho["tenant"]["nome_fantasia"]},
            "vendedor": {"id": aparelho["id_funcionario"], "nome": aparelho["nome_vendedor"]},
            "pin_offline_dias": config.PIN_OFFLINE_DIAS}


@app.post("/api/sair")
async def sair(request: Request, aparelho: dict = Depends(sessao.aparelho_atual)):
    sessao.revogar(request.headers.get("authorization", "").removeprefix("Bearer ").strip())
    return {"ok": True}


# ---- Catálogo no celular ----------------------------------------------------------------------------------------
@app.post("/api/sincronizar")
async def sincronizar(aparelho: dict = Depends(sessao.aparelho_atual)):
    """Pede ao servidor para atualizar a cópia do catálogo (lê do ERP só o que estiver vencido) e diz como está."""
    catalogo.disparar(aparelho["tenant"])
    return catalogo.estado(aparelho["cnpj"])


@app.get("/api/sincronizar/estado")
async def sincronizar_estado(aparelho: dict = Depends(sessao.aparelho_atual)):
    return catalogo.estado(aparelho["cnpj"])


@app.get("/api/dados/{tabela}")
async def dados(tabela: str, cursor: str = "", aparelho: dict = Depends(sessao.aparelho_atual)):
    """O que mudou na cópia depois do cursor que o celular guardou (página de até 2000 linhas)."""
    if tabela not in catalogo.TABELAS or len(cursor) > 80:
        return _erro(404, "Tabela desconhecida.")
    return catalogo.mudancas(aparelho["cnpj"], tabela, cursor)


# ---- App (PWA) -------------------------------------------------------------------------------------------------
@app.get("/sw.js")
async def service_worker():
    # Na raiz para controlar o app inteiro; nunca em cache, senão o celular não vê atualização.
    corpo = (ESTATICOS / "sw.js").read_text(encoding="utf-8").replace("__VERSAO__", VERSAO)
    return Response(corpo, media_type="text/javascript", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/")
async def inicio():
    return FileResponse(ESTATICOS / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")
