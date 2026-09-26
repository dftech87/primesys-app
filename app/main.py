from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import init_db
from app.routers import admin, auth, dashboard

app = FastAPI(title="PrimeSys Dashboard")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

init_db()

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
