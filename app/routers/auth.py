from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import limite
from app.security import normalize_cnpj
from app.tenants import ERRO_INDISPONIVEL, authenticate

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _destino_seguro(next_url: str | None) -> str:
    """Só aceita caminhos internos (começando com '/', mas não '//') para evitar
    que um link de login manipulado redirecione pra um site externo."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/produtos"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/produtos"):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "next": _destino_seguro(next)},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    cnpj: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...),
    next: str = Form("/produtos"),
):
    destino = _destino_seguro(next)

    def recusar(mensagem: str, status: int):
        return templates.TemplateResponse(request, "login.html", {"error": mensagem, "next": destino}, status_code=status)

    # Limite de tentativas: por CNPJ+e-mail (principal) e por endereço de internet. Bloqueado, nem consulta o ERP.
    ip = limite.ip_do_cliente(request)
    chave_usuario = f"login:{normalize_cnpj(cnpj)}:{email.strip().lower()}"
    chave_ip = f"ip:{ip}" if ip else None
    espera = max(
        limite.segundos_bloqueado(chave_usuario, limite.LIMITE_POR_USUARIO),
        limite.segundos_bloqueado(chave_ip, limite.LIMITE_POR_IP) if chave_ip else 0,
    )
    if espera:
        return recusar(limite.mensagem_bloqueio(espera), 429)

    tenant, erro = await authenticate(cnpj, email, senha)
    if tenant is None:
        if erro != ERRO_INDISPONIVEL:   # falha do ERP ou do nosso banco não é senha errada
            limite.registrar_falha(chave_usuario)
            if chave_ip:
                limite.registrar_falha(chave_ip)
        return recusar(erro, 401)

    limite.limpar(chave_usuario)
    request.session["cnpj"] = tenant["cnpj"]
    return RedirectResponse(destino, status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
