from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import init_db
from app.routers import admin, auth, dashboard, produtos
from app.tenants import create_tenant

app = FastAPI(title="PrimeSys Dashboard")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
# O Android exige esse arquivo exatamente em /.well-known/assetlinks.json (raiz do
# domínio) para verificar o app instalado como "dono" do site e abrir sem barra
# de navegador (Trusted Web Activity totalmente confiável).
app.mount("/.well-known", StaticFiles(directory="app/static/well-known"), name="well-known")

init_db()

# Bootstrap do primeiro cliente (DFTECH) a partir das variáveis de ambiente, para
# que um deploy novo (banco vazio) já tenha um tenant utilizável sem precisar de
# acesso a shell no servidor. Idempotente: apenas atualiza se já existir.
if settings.meuerp_cnpj and settings.meuerp_token:
    create_tenant(cnpj=settings.meuerp_cnpj, nome_fantasia="DFTECH", api_token=settings.meuerp_token)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(produtos.router)
