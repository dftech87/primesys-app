"""/admin/liberacoes: liga a liberação de permissão do caixa por empresa e escolhe quais perfis do ERP podem aprovar."""

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import liberacoes, push
from app.cache import cached
from app.database import db_session
from app.meuerp_client import MeuERPClient, nome_do_balde
from app.routers.admin import _avisar, _is_admin
from app.security import normalize_cnpj
from app.tenants import get_tenant_by_cnpj, list_tenants

router = APIRouter(prefix="/admin/liberacoes")
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

PERFIS_VALEM = 600          # s: a lista de perfis quase não muda; guardada, a página não chama o ERP a cada abertura
ESPERA_MAXIMA_PERFIS = 4.0  # s: a página nunca espera mais que isso pelo ERP (a fila de cota pode atrasar)


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
                "push_ligado": push.ligado(), "push_faltando": push.faltando(), "cfg": None, "perfis": [], "estado": None, "decisoes": [], "teste": None,
                "teste_horas": liberacoes.TESTE_HORAS_PADRAO, "teste_horas_max": liberacoes.TESTE_HORAS_MAXIMO}
    tenant = get_tenant_by_cnpj(contexto["cnpj"]) if contexto["cnpj"] else None
    if tenant is not None and tenant.get("api_token"):
        cfg = liberacoes.config(tenant["cnpj"])
        contexto["cfg"] = cfg
        try:
            contexto["perfis"] = await asyncio.wait_for(
                cached((tenant["cnpj"], "liberacoes-perfis"), PERFIS_VALEM, lambda: _perfis_do_erp(tenant), obsoleto_ate=24 * 3600),
                ESPERA_MAXIMA_PERFIS)
        except asyncio.TimeoutError:   # a busca continua por trás e a próxima abertura já encontra a lista pronta
            contexto["perfis"] = [{"codigo": c, "nome": f"Perfil {c}"} for c in cfg["perfis"]]
            contexto["erro"] = contexto["erro"] or "O ERP está demorando para listar os perfis; mostrando os já escolhidos. Recarregue em alguns segundos."
        except Exception:
            logger.exception("lista de perfis indisponível (empresa %s)", tenant["cnpj"])
            contexto["erro"] = contexto["erro"] or "Não foi possível ler os perfis do ERP agora. Tente de novo em instantes."
        resumo = liberacoes.resumo(tenant["cnpj"])
        contexto["estado"] = {"pendentes": len(resumo["itens"]), "atualizado_ha": resumo["atualizado_ha"], "erro": resumo["erro"], "motivo": resumo["motivo"],
                              "celulares": liberacoes.quantos_inscritos(tenant["cnpj"]), "codigo_token": nome_do_balde(tenant["api_token"]), "chamadas_por_min": round(60 / liberacoes.intervalo(tenant["cnpj"]), 1)}
        dados_teste = liberacoes.teste(tenant["cnpj"])
        if dados_teste is not None:
            situacao = "andamento" if liberacoes.teste_rodando(tenant["cnpj"]) else ("cumprido" if dados_teste["encerrado_em"] else "vencido")
            contexto["teste"] = {"situacao": situacao, "horas_restantes": max(0, round((dados_teste["expira_em"] - time.time()) / 3600, 1))}
        else:
            contexto["teste"] = None
        contexto["decisoes"] = _ultimas_decisoes(tenant["cnpj"])
    return templates.TemplateResponse(request, "admin_liberacoes.html", contexto)


@router.get("", response_class=HTMLResponse)
async def tela(request: Request, c: str = ""):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return await _tela(request, c)


@router.post("")
async def salvar(request: Request, cnpj: str = Form(""), ativo: str = Form(""), intervalo: int = Form(liberacoes.INTERVALO_PADRAO),
                 hora_inicial: int = Form(liberacoes.HORA_INICIAL_PADRAO), hora_final: int = Form(liberacoes.HORA_FINAL_PADRAO),
                 acao: str = Form(""), horas: int = Form(liberacoes.TESTE_HORAS_PADRAO)):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    formulario = await request.form()
    cnpj = normalize_cnpj(cnpj)
    tenant = get_tenant_by_cnpj(cnpj)
    if tenant is None:
        return await _tela(request, cnpj, "Escolha uma empresa cadastrada.")
    perfis = [int(p) for p in formulario.getlist("perfil") if str(p).isdigit()]
    if acao == "teste":
        # Teste de uma notificação: liga a empresa JÁ em modo teste (o teste é gravado antes de ligar, sem janela em que a função
        # funcione de verdade). Só avisa; não aprova nada; para sozinha depois do primeiro aviso entregue.
        if not perfis:
            return await _tela(request, cnpj, "Marque pelo menos um perfil que vai receber o aviso antes de iniciar o teste.")
        liberacoes.ligar_teste(cnpj, horas)
        liberacoes.salvar_config(cnpj, True, perfis, intervalo, hora_inicial, hora_final)
        _avisar(request, "Teste iniciado: o primeiro pedido de liberação gera um aviso no celular e a consulta ao ERP para sozinha. Nada é aprovado pelo app.")
        return RedirectResponse(f"/admin/liberacoes?c={cnpj}", status_code=303)
    ligar = ativo == "1"
    if ligar and not perfis:
        return await _tela(request, cnpj, "Marque pelo menos um perfil que pode aprovar antes de ligar.")
    liberacoes.salvar_config(cnpj, ligar, perfis, intervalo, hora_inicial, hora_final)
    _avisar(request, "Liberações do caixa " + ("ligadas" if ligar else "desligadas") + " para esta empresa.")
    return RedirectResponse(f"/admin/liberacoes?c={cnpj}", status_code=303)


_ultima_leitura_manual: dict[str, float] = {}
ESPERA_ENTRE_LEITURAS_MANUAIS = 10   # s: o botão não vira uma forma de martelar o ERP (cada clique é 1 chamada na cota da empresa)


@router.post("/testar-leitura")
async def testar_leitura(request: Request, cnpj: str = Form("")):
    """1 única chamada de leitura (limite 1) para saber se a chave desta empresa enxerga a fila de liberações. Não grava nada no ERP."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    cnpj = normalize_cnpj(cnpj)
    tenant = get_tenant_by_cnpj(cnpj)
    if tenant is None or not tenant.get("api_token"):
        return await _tela(request, cnpj, "Escolha uma empresa cadastrada.")
    agora = time.monotonic()
    if agora - _ultima_leitura_manual.get(cnpj, -1e9) < ESPERA_ENTRE_LEITURAS_MANUAIS:
        return await _tela(request, cnpj, "Espere alguns segundos antes de testar de novo.")
    _ultima_leitura_manual[cnpj] = agora
    try:
        resposta = await asyncio.wait_for(MeuERPClient(tenant["api_token"]).get(liberacoes.CAMINHO_ERP, params={"page": 1, "limit": 1}), 12)
    except httpx.HTTPStatusError as erro:
        codigo = erro.response.status_code
        # texto que o próprio ERP devolveu junto com a recusa (só no /admin; limitado e sem quebras de linha)
        texto_erp = ""
        try:
            corpo = erro.response.json()
            texto_erp = str((corpo.get("mensagem") or corpo.get("message") or corpo.get("detail") or "") if isinstance(corpo, dict) else "")
        except Exception:
            texto_erp = (erro.response.text or "")[:200]
        texto_erp = " ".join(texto_erp.split())[:200]
        detalhe = f" Mensagem do ERP: “{texto_erp}”." if texto_erp else " O ERP não mandou mensagem junto com a recusa."
        dica = (" Em geral o usuário do ERP dono desta chave não tem acesso a Liberação de permissão."
                if codigo in (401, 403) else "")
        return await _tela(request, cnpj, f"O ERP recusou a leitura da fila (HTTP {codigo}).{detalhe}{dica}")
    except Exception as erro:   # só o tipo do erro: o texto pode trazer endereço
        return await _tela(request, cnpj, f"O ERP não respondeu à leitura da fila ({type(erro).__name__}). Tente de novo em instantes.")
    total = resposta.get("total") if isinstance(resposta, dict) else None
    _avisar(request, "Leitura da fila OK: o ERP aceitou a chave desta empresa." + (f" Pedidos pendentes agora: {total}." if total is not None else ""))
    return RedirectResponse(f"/admin/liberacoes?c={cnpj}", status_code=303)


@router.post("/teste/encerrar")
async def encerrar_teste(request: Request, cnpj: str = Form("")):
    """Sai do modo teste e DESLIGA a função na empresa (para voltar a aprovar pelo app é preciso ligar de novo, de propósito)."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    cnpj = normalize_cnpj(cnpj)
    if get_tenant_by_cnpj(cnpj) is None:
        return await _tela(request, cnpj, "Escolha uma empresa cadastrada.")
    cfg = liberacoes.config(cnpj)
    liberacoes.salvar_config(cnpj, False, cfg["perfis"], cfg["intervalo"], cfg["hora_inicial"], cfg["hora_final"])
    liberacoes.sair_do_teste(cnpj)
    _avisar(request, "Teste encerrado e liberações desligadas nesta empresa.")
    return RedirectResponse(f"/admin/liberacoes?c={cnpj}", status_code=303)
