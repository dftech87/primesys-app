from fastapi import Request
from fastapi.responses import JSONResponse

from app import acessos
from app.tenants import get_tenant_by_cnpj


def current_tenant(request: Request) -> dict | None:
    """Empresa da sessão. Sessão sem os acessos do perfil (de antes deles existirem) conta como não logada."""
    cnpj = request.session.get("cnpj")
    if not cnpj or acessos.na_sessao(request) is None:
        return None
    return get_tenant_by_cnpj(cnpj)


def sem_permissao() -> JSONResponse:
    return JSONResponse({"detail": acessos.ERRO_SEM_PERMISSAO}, status_code=403)
