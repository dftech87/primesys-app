import sqlite3

from fastapi import Request

from app.tenants import get_tenant_by_cnpj


def current_tenant(request: Request) -> sqlite3.Row | None:
    cnpj = request.session.get("cnpj")
    return get_tenant_by_cnpj(cnpj) if cnpj else None
