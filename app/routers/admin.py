from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import asyncio
import secrets

from app import limite
from app.config import ADMIN_ATIVO, settings
from app.security import normalize_cnpj
from app.tenants import create_tenant, empresa_do_token, list_tenants, set_tenant_ativo

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _mask_token(token: str) -> str:
    # Só os 4 últimos: o suficiente para reconhecer qual token é, sem expor o começo dele.
    return f"••••{token[-4:]}" if len(token) > 8 else "••••"


async def _tenants_view() -> list[dict]:
    """Lista para a tela, já com a conferência de cada token: ele é da empresa do CNPJ? Está repetido em outro
    cadastro? (A pergunta é feita ao próprio ERP, 1 chamada por cliente; o token em si nunca aparece inteiro.)"""
    tenants = list_tenants()
    donos = await asyncio.gather(*(empresa_do_token(t["api_token"]) if t["api_token"] else asyncio.sleep(0) for t in tenants))
    quem_usa: dict[str, list[str]] = {}
    for t in tenants:
        if t["api_token"]:
            quem_usa.setdefault(t["api_token"], []).append(t["nome_fantasia"])

    resultado = []
    for t, dono in zip(tenants, donos):
        problemas, cor = [], "#166534"
        repetido_com = [n for n in quem_usa.get(t["api_token"], []) if n != t["nome_fantasia"]] if t["api_token"] else []
        if not t["api_token"]:
            problemas, cor = ["Token ilegível (chave de criptografia ausente ou diferente)"], "#991b1b"
        else:
            if repetido_com:
                problemas.append("Token REPETIDO com: " + ", ".join(repetido_com))
            if dono is None:
                problemas.append("Não foi possível conferir no ERP (token inválido ou ERP fora do ar)")
            elif dono != normalize_cnpj(t["cnpj"]):
                problemas.append(f"Token é da empresa de CNPJ {dono}, não deste CNPJ")
            cor = "#991b1b" if problemas else "#166534"
        resultado.append({
            **dict(t),
            "api_token": _mask_token(t["api_token"]),
            "conf_texto": "; ".join(problemas) if problemas else "Confere com o CNPJ",
            "conf_cor": cor,
        })
    return resultado


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
    return templates.TemplateResponse(request, "admin.html", {"tenants": await _tenants_view(), "message": None})


@router.post("/tenants")
async def admin_create_tenant(
    request: Request,
    cnpj: str = Form(...),
    nome_fantasia: str = Form(...),
    api_token: str = Form(...),
):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    token = api_token.strip()
    # Confere no próprio ERP de quem é o token antes de gravar: token trocado = dados de outra empresa na tela.
    dono = await empresa_do_token(token)
    if dono is None:
        mensagem = "NÃO cadastrado: o ERP não reconheceu esse token. Confira se ele foi copiado inteiro e tente de novo."
    elif dono != normalize_cnpj(cnpj):
        mensagem = f"NÃO cadastrado: esse token pertence à empresa de CNPJ {dono}, e não ao CNPJ informado. Use o token da empresa certa."
    else:
        create_tenant(cnpj=cnpj, nome_fantasia=nome_fantasia, api_token=token)
        mensagem = f"Cliente {nome_fantasia} cadastrado com sucesso (token conferido com o CNPJ no ERP)."
    return templates.TemplateResponse(request, "admin.html", {"tenants": await _tenants_view(), "message": mensagem})


@router.post("/tenants/{cnpj}/desativar")
async def admin_desativar_tenant(request: Request, cnpj: str):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    set_tenant_ativo(cnpj, ativo=False)
    return templates.TemplateResponse(
        request, "admin.html", {"tenants": await _tenants_view(), "message": "Cliente desativado."}
    )


@router.post("/tenants/{cnpj}/reativar")
async def admin_reativar_tenant(request: Request, cnpj: str):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    set_tenant_ativo(cnpj, ativo=True)
    return templates.TemplateResponse(
        request, "admin.html", {"tenants": await _tenants_view(), "message": "Cliente reativado."}
    )
