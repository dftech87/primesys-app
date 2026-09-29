from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.tenants import create_tenant, list_tenants, set_tenant_ativo

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}...{token[-4:]}"


def _tenants_view() -> list[dict]:
    return [{**dict(t), "api_token": _mask_token(t["api_token"])} for t in list_tenants()]


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})


@router.post("/login")
async def admin_login_submit(request: Request, usuario: str = Form(...), senha: str = Form(...)):
    if usuario != settings.admin_user or senha != settings.admin_password:
        return templates.TemplateResponse(
            request, "admin_login.html", {"error": "Usuário ou senha inválidos."}, status_code=401
        )
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
