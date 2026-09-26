from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.tenants import authenticate

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request, cnpj: str = Form(...), email: str = Form(...), senha: str = Form(...)
):
    tenant, erro = await authenticate(cnpj, email, senha)
    if tenant is None:
        return templates.TemplateResponse(request, "login.html", {"error": erro}, status_code=401)
    request.session["cnpj"] = tenant["cnpj"]
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
