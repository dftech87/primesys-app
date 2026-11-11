"""/admin/liberacoes: liga a liberação de permissão do caixa por empresa e escolhe quais perfis do ERP podem aprovar."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import liberacoes, push
from app.database import db_session
from app.meuerp_client import MeuERPClient
from app.routers.admin import _avisar, _is_admin
from app.security import normalize_cnpj
from app.tenants import get_tenant_by_cnpj, list_tenants

router = APIRouter(prefix="/admin/liberacoes")
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


async def _perfis_do_erp(tenant: dict) -> list[dict]:
    """Perfis de usuário da empresa (código e nome), para o administrador marcar quais aprovam."""
    client = MeuERPClient(tenant["api_token"])
    perfis: list[dict] = []
    for pagina in range(1, 4):
        resposta = await client.get("/api/usuario-perfil/v1", params={"page": pagina, "limit": 100})
        perfis += [{"codigo": int(p["codigo"]), "nome": str(p.get("descricao") or "").strip()} for p in resposta.get("items") or [] if p.get("ativo") != "N"]
        if not resposta.get("hasNext"):
            break
    return sorted(perfis, key=lambda p: p["nome"].lower())


def _ultimas_decisoes(cnpj: str) -> list[dict]:
    try:
        with db_session() as conn:
            return [dict(l) for l in conn.execute(
                "SELECT criado_em, nome, decisao, permissao, caixa, resultado FROM liberacoes_decisoes WHERE cnpj = ? ORDER BY criado_em DESC LIMIT 10", (cnpj,)
            ).fetchall()]
    except Exception:
        logger.exception("não foi possível ler as decisões de liberação")
        return []


async def _tela(request: Request, cnpj: str = "", erro: str = ""):
    empresas = sorted(({"cnpj": t["cnpj"], "nome": t["nome_fantasia"]} for t in list_tenants()), key=lambda e: e["nome"].lower())
    contexto = {"empresas": empresas, "cnpj": normalize_cnpj(cnpj), "erro": erro, "aviso": request.session.pop("aviso", None),
                "push_ligado": push.ligado(), "cfg": None, "perfis": [], "estado": None, "decisoes": []}
    tenant = get_tenant_by_cnpj(contexto["cnpj"]) if contexto["cnpj"] else None
    if tenant is not None and tenant.get("api_token"):
        cfg = liberacoes.config(tenant["cnpj"])
        contexto["cfg"] = cfg
        try:
            contexto["perfis"] = await _perfis_do_erp(tenant)
        except Exception:
            logger.exception("lista de perfis indisponível (empresa %s)", tenant["cnpj"])
            contexto["erro"] = contexto["erro"] or "Não foi possível ler os perfis do ERP agora. Tente de novo em instantes."
        resumo = liberacoes.resumo(tenant["cnpj"])
        contexto["estado"] = {"pendentes": len(resumo["itens"]), "atualizado_ha": resumo["atualizado_ha"], "erro": resumo["erro"],
                              "celulares": liberacoes.quantos_inscritos(tenant["cnpj"]), "chamadas_por_min": round(60 / cfg["intervalo"], 1)}
        contexto["decisoes"] = _ultimas_decisoes(tenant["cnpj"])
    return templates.TemplateResponse(request, "admin_liberacoes.html", contexto)


@router.get("", response_class=HTMLResponse)
async def tela(request: Request, c: str = ""):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return await _tela(request, c)


@router.post("")
async def salvar(request: Request, cnpj: str = Form(""), ativo: str = Form(""), intervalo: int = Form(liberacoes.INTERVALO_PADRAO),
                 hora_inicial: int = Form(liberacoes.HORA_INICIAL_PADRAO), hora_final: int = Form(liberacoes.HORA_FINAL_PADRAO)):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    formulario = await request.form()
    cnpj = normalize_cnpj(cnpj)
    tenant = get_tenant_by_cnpj(cnpj)
    if tenant is None:
        return await _tela(request, cnpj, "Escolha uma empresa cadastrada.")
    perfis = [int(p) for p in formulario.getlist("perfil") if str(p).isdigit()]
    ligar = ativo == "1"
    if ligar and not perfis:
        return await _tela(request, cnpj, "Marque pelo menos um perfil que pode aprovar antes de ligar.")
    liberacoes.salvar_config(cnpj, ligar, perfis, intervalo, hora_inicial, hora_final)
    _avisar(request, "Liberações do caixa " + ("ligadas" if ligar else "desligadas") + " para esta empresa.")
    return RedirectResponse(f"/admin/liberacoes?c={cnpj}", status_code=303)
