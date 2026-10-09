from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import versao
from app.config import COOKIE_SEGURO, SESSAO_MAX_IDADE, settings
from app.database import db_session, init_db
from app.routers import admin, auth, dashboard, painel_mobile, produtos
from app.tenants import create_tenant_if_missing, criptografar_tokens_existentes



class _GZipSemServiceWorker(GZipMiddleware):
    """Comprime as respostas (ajuda no 4G), exceto o service worker: ele é a peça mais sensível do app
    instalado e continua sendo entregue exatamente como antes, sem nenhuma camada no meio."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/static/sw.js":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app = FastAPI(title="PrimeSys Dashboard")
app.add_middleware(
    SessionMiddleware, secret_key=settings.session_secret, https_only=COOKIE_SEGURO, same_site="lax", max_age=SESSAO_MAX_IDADE
)
app.add_middleware(_GZipSemServiceWorker, minimum_size=1024)


@app.middleware("http")
async def api_sem_cache(request, call_next):
    """Respostas de /api/* são de UMA empresa: nunca podem ficar guardadas no navegador ou em intermediários."""
    resposta = await call_next(request)
    if request.url.path.startswith("/api/") and "cache-control" not in resposta.headers:
        resposta.headers["Cache-Control"] = "no-store"
        resposta.headers["Vary"] = "Cookie"
    return resposta


@app.api_route("/saude", methods=["GET", "HEAD"])
async def saude():
    """Verificação de saúde para o monitoramento externo e para o deploy do Railway: responde 200 só se o app
    está de pé e o banco de clientes abre. Não consulta o ERP (gastaria a cota de chamadas) e não revela nada."""
    try:
        with db_session() as conn:
            conn.execute("SELECT 1 FROM empresas LIMIT 1").fetchone()
    except Exception:
        return JSONResponse({"ok": False}, status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/versao")
async def versao_atual():
    """Número da versão no ar (hash curto). O app compara com o da tela carregada e avisa quando mudou."""
    return JSONResponse({"versao": versao.VERSAO}, headers={"Cache-Control": "no-store"})


@app.get("/static/sw.js")
async def service_worker():
    # Registrado antes do mount de /static abaixo, e com cache explicitamente
    # desligado: esse arquivo já causou dor de cabeça de app ficando preso numa
    # versão antiga porque alguma camada (Android, navegador) guardava uma cópia
    # velha dele. Sem isso, um app instalado pode nunca ver as atualizações.
    return FileResponse(
        "app/static/sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory="app/static"), name="static")
# O Android exige esse arquivo exatamente em /.well-known/assetlinks.json (raiz do
# domínio) para verificar o app instalado como "dono" do site e abrir sem barra
# de navegador (Trusted Web Activity totalmente confiável).
app.mount("/.well-known", StaticFiles(directory="app/static/well-known"), name="well-known")

init_db()

# Bootstrap do primeiro cliente (DFTECH) a partir das variáveis de ambiente, para
# que um deploy novo (banco vazio) já tenha um tenant utilizável sem precisar de
# acesso a shell no servidor. Só cria se ainda não existir: nunca sobrescreve o token que o /admin gravou.
if settings.meuerp_cnpj and settings.meuerp_token:
    create_tenant_if_missing(cnpj=settings.meuerp_cnpj, nome_fantasia="DFTECH", api_token=settings.meuerp_token)

# Com TOKEN_ENCRYPTION_KEY definida, converte para criptografado os tokens que ainda estão em texto simples.
criptografar_tokens_existentes()

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(produtos.router)
app.include_router(painel_mobile.router)
