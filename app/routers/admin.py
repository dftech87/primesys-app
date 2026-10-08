from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import secrets

from app import limite
from app.config import ADMIN_ATIVO, settings
from app.tenants import create_tenant, list_tenants, set_tenant_ativo

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _mask_token(token: str) -> str:
    # Só os 4 últimos: o suficiente para reconhecer qual token é, sem expor o começo dele.
    return f"••••{token[-4:]}" if len(token) > 8 else "••••"


def _tenants_view() -> list[dict]:
    return [{**dict(t), "api_token": _mask_token(t["api_token"])} for t in list_tenants()]


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})


@router.post("/login")
async def admin_login_submit(request: Request, usuario: str = Form(...), senha: str = Form(...)):
    def recusar(mensagem: str, status: int):
        return templates.TemplateResponse(request, "admin_login.html", {"error": mensagem}, status_code=status)

    if not ADMIN_ATIVO:
        return recusar("Acesso de administração desativado: defina ADMIN_PASSWORD (uma senha forte) no servidor.", 503)

    chave = f"admin:{limite.ip_do_cliente(request) or 'sem-ip'}"
    espera = limite.segundos_bloqueado(chave, limite.LIMITE_ADMIN)
    if espera:
        return recusar(limite.mensagem_bloqueio(espera), 429)

    usuario_ok = secrets.compare_digest(usuario.encode(), settings.admin_user.encode())
    senha_ok = secrets.compare_digest(senha.encode(), settings.admin_password.encode())
    if not (usuario_ok and senha_ok):
        limite.registrar_falha(chave)
        return recusar("Usuário ou senha inválidos.", 401)
    limite.limpar(chave)
    request.session["is_admin"] = True
    return RedirectResponse("/admin", status_code=303)


@router.get("/logout")
async def admin_logout(request: Request):
    request.session.pop("is_admin", None)
    return RedirectResponse("/admin/login", status_code=303)


@router.get("", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {"tenants": _tenants_view(), "message": None})


@router.post("/tenants")
async def admin_create_tenant(
    request: Request,
    cnpj: str = Form(...),
    nome_fantasia: str = Form(...),
    api_token: str = Form(...),
):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    create_tenant(cnpj=cnpj, nome_fantasia=nome_fantasia, api_token=api_token.strip())
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"tenants": _tenants_view(), "message": f"Cliente {nome_fantasia} cadastrado com sucesso."},
    )


@router.post("/tenants/{cnpj}/desativar")
async def admin_desativar_tenant(request: Request, cnpj: str):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    set_tenant_ativo(cnpj, ativo=False)
    return templates.TemplateResponse(
        request, "admin.html", {"tenants": _tenants_view(), "message": "Cliente desativado."}
    )


@router.post("/tenants/{cnpj}/reativar")
async def admin_reativar_tenant(request: Request, cnpj: str):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    set_tenant_ativo(cnpj, ativo=True)
    return templates.TemplateResponse(
        request, "admin.html", {"tenants": _tenants_view(), "message": "Cliente reativado."}
    )
