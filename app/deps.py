from fastapi import Request
from fastapi.responses import JSONResponse

from app import acessos, desempenho
from app.tenants import get_tenant_by_cnpj


def current_tenant(request: Request) -> dict | None:
    """Empresa da sessão. Sessão sem os acessos do perfil (de antes deles existirem) conta como não logada."""
    cnpj = request.session.get("cnpj")
    if not cnpj or acessos.na_sessao(request) is None:
        return None
    desempenho.empresa(cnpj)
    return get_tenant_by_cnpj(cnpj)


async def revalidar_acessos(request: Request) -> None:
    """Dependência das rotas protegidas: com o app aberto, reconfere as permissões do perfil a cada 5 minutos (e quando o
    /admin manda atualizar), para um bloqueio feito no ERP valer sem a pessoa reabrir o app. Só lê a sessão; a empresa só
    é buscada quando a hora de reconferir chegou."""
    if not acessos.precisa_revalidar(request):
        return
    tenant = get_tenant_by_cnpj(request.session.get("cnpj"))
    if tenant is not None:
        await acessos.revalidar(request, tenant)


def sem_permissao() -> JSONResponse:
    return JSONResponse({"detail": acessos.ERRO_SEM_PERMISSAO}, status_code=403)
