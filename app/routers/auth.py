from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.tenants import authenticate

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
    tenant, erro = await authenticate(cnpj, email, senha)
    if tenant is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": erro, "next": destino}, status_code=401
        )
    request.session["cnpj"] = tenant["cnpj"]
    return RedirectResponse(destino, status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
