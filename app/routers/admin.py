from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import asyncio
import secrets

from app import acessos, backup, consumo, desempenho, estado_api, limite
from app.cache import cached
from app.meuerp_client import MeuERPClient, nome_do_balde
from app.sqlquery import executar
from app.config import ADMIN_ATIVO, settings
from app.security import normalize_cnpj
from app.tenants import (
    create_tenant, list_tenants, set_tenant_ativo, set_tenant_nome,
    get_tenant_by_cnpj, set_tenant_token, set_vendas_liberado,
)

import logging

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _cnpj_formatado(cnpj: str) -> str:
    d = normalize_cnpj(cnpj)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}" if len(d) == 14 else d


def _data_br(texto) -> str:
    t = str(texto or "")
    return f"{t[8:10]}/{t[5:7]}/{t[:4]}" if len(t) >= 10 else t


def _avisar(request: Request, texto: str, tipo: str = "ok") -> None:
    """Mensagem para a próxima tela (depois do redirecionamento), guardada na sessão assinada."""
    request.session["aviso"] = {"texto": texto, "tipo": tipo}


def _voltar(cnpj: str = "", novo: bool = False) -> RedirectResponse:
    destino = "/admin?novo=1" if novo else (f"/admin?c={normalize_cnpj(cnpj)}" if cnpj else "/admin")
    return RedirectResponse(destino, status_code=303)


async def _conferencia(cliente: dict, todos: list[dict], api: dict) -> tuple[str, str]:
    """O token do cliente aberto é da empresa do CNPJ? Está repetido em outro cadastro? (`api`: teste já feito, 1 consulta ao ERP.)"""
    if not cliente["api_token"]:
        return "Token ilegível (chave de criptografia ausente ou diferente)", "ruim"
    repetido = [t["nome_fantasia"] for t in todos if t["api_token"] == cliente["api_token"] and t["cnpj"] != cliente["cnpj"]]
    problemas = []
    if repetido:
        problemas.append("Token repetido com: " + ", ".join(repetido))
    if api["estado"] == "outra":
        problemas.append(f"Token é da empresa de CNPJ {_cnpj_formatado(api['cnpj'])}, não deste CNPJ")
    elif api["estado"] != "ok":
        problemas.append("Não foi possível conferir a empresa do token: veja a situação da API acima")
    if problemas:
        return "; ".join(problemas), "ruim"
    lojas = await _empresas_no_banco(cliente)
    if lojas and lojas > 1:   # o token está certo; é só um aviso
        return (f"Atenção: o ERP deste cliente tem {lojas} empresas (lojas) no mesmo banco. Produtos, preços e estoque já usam a loja "
                "deste CNPJ, mas o Dashboard do Prime Info (vendas, caixa, compras, financeiro) soma as lojas."), "aviso"
    return "Confere com o CNPJ", "bom"


EMPRESAS_NO_BANCO_VALE = 24 * 3600   # muda raríssimo: 1 consulta por cliente por dia, só ao abrir o cliente no /admin
ESPERA_EMPRESAS_NO_BANCO = 4.0       # a página do /admin nunca espera mais que isso pelo ERP


async def _empresas_no_banco(cliente: dict) -> int | None:
    """Quantas empresas (lojas) existem no banco do ERP deste cliente. None se não deu para saber agora."""
    async def contar() -> int | None:
        linhas = await executar(MeuERPClient(cliente["api_token"]), "select count(*) as n from empresa", max_paginas=1)
        return int(linhas[0]["n"]) if linhas else None
    try:
        return await asyncio.wait_for(cached((cliente["cnpj"], "empresas-no-banco"), EMPRESAS_NO_BANCO_VALE, contar,
                                             guardar_se=lambda v: v is not None), ESPERA_EMPRESAS_NO_BANCO)
    except Exception:
        logger.warning("não foi possível contar as empresas no banco do ERP (cliente %s)", cliente["cnpj"])
        return None


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})


@router.post("/login")
async def admin_login_submit(request: Request, usuario: str = Form(...), senha: str = Form(...)):
    def recusar(mensagem: str, status: int):
        return templates.TemplateResponse(request, "admin_login.html", {"error": mensagem}, status_code=status)

    if not ADMIN_ATIVO:
        return recusar("Acesso de administração desativado: defina ADMIN_PASSWORD (uma senha forte) no servidor.", 503)

    chave = f"admin:{limite.ip_do_cliente(request) or 'sem-ip'}"
    espera = limite.segundos_bloqueado(chave, limite.LIMITE_ADMIN)
    if espera:
        return recusar(limite.mensagem_bloqueio(espera), 429)

    usuario_ok = secrets.compare_digest(usuario.encode(), settings.admin_user.encode())
    senha_ok = secrets.compare_digest(senha.encode(), settings.admin_password.encode())
    if not (usuario_ok and senha_ok):
        limite.registrar_falha(chave)
        return recusar("Usuário ou senha inválidos.", 401)
    limite.limpar(chave)
    request.session["is_admin"] = True
    return RedirectResponse("/admin", status_code=303)


@router.get("/logout")
async def admin_logout(request: Request):
    request.session.pop("is_admin", None)
    return RedirectResponse("/admin/login", status_code=303)


@router.get("", response_class=HTMLResponse)
async def admin_home(request: Request, c: str = "", novo: int = 0):
    """Lista de clientes à esquerda e, à direita, o cliente aberto (ou o cadastro de um novo).
    A página só testa a API do cliente aberto; a lista pede a situação dos outros depois, em /admin/api/{cnpj}."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    todos = list_tenants()
    clientes = sorted(
        ({**t, "cnpj_fmt": _cnpj_formatado(t["cnpj"]), "desde": _data_br(t["criado_em"])} for t in todos),
        key=lambda t: (not t["ativo"], (t["nome_fantasia"] or "").upper()),
    )
    aberto = None
    cnpj = normalize_cnpj(c)
    if not novo:
        aberto = next((t for t in clientes if t["cnpj"] == cnpj), None) or (clientes[0] if clientes else None)
    if aberto:
        api = await estado_api.do_cliente(aberto, agora=True)
        texto, classe = await _conferencia(aberto, todos, api)
        aberto = {**aberto, "conf_texto": texto, "conf_classe": classe, "api": estado_api.para_tela(api)}
    contexto = {"clientes": [{k: v for k, v in t.items() if k != "api_token"} for t in clientes],
                "aberto": {k: v for k, v in aberto.items() if k != "api_token"} if aberto else None,
                "novo": bool(novo) or not clientes, "aviso": request.session.pop("aviso", None),
                "backup_configurado": backup.configurado()}
    return templates.TemplateResponse(request, "admin.html", contexto)


@router.get("/api/{cnpj}")
async def admin_estado_da_api(request: Request, cnpj: str, agora: int = 0):
    """Situação da API do ERP de um cliente (1 leitura no ERP, guardada por 2 minutos; `agora=1` refaz o teste)."""
    if not _is_admin(request):
        return JSONResponse({"erro": "Sessão expirada. Entre de novo."}, status_code=401)
    cliente = next((t for t in list_tenants() if t["cnpj"] == normalize_cnpj(cnpj)), None)
    if cliente is None:
        return JSONResponse({"erro": "Cliente não encontrado."}, status_code=404)
    return JSONResponse(estado_api.para_tela(await estado_api.do_cliente(cliente, agora=bool(agora))), headers={"Cache-Control": "no-store"})


@router.post("/tenants/consultar")
async def admin_consultar_token(request: Request, api_token: str = Form(...)):
    """Novo cliente: diz de qual empresa é o token (CNPJ e nome, segundo o próprio ERP) e se já está cadastrada."""
    if not _is_admin(request):
        return JSONResponse({"erro": "Sessão expirada. Entre de novo."}, status_code=401)
    dados = await estado_api.testar(api_token.strip())
    if dados["estado"] != "ok":
        return JSONResponse({"erro": estado_api.explicar(dados)}, status_code=422)
    existente = next((t for t in list_tenants() if t["cnpj"] == dados["cnpj"]), None)
    return {"cnpj": dados["cnpj"], "cnpj_fmt": _cnpj_formatado(dados["cnpj"]), "nome": dados["fantasia"] or dados["razao"],
            "ja_cadastrado": existente is not None, "nome_cadastrado": existente["nome_fantasia"] if existente else ""}


@router.post("/tenants")
async def admin_create_tenant(
    request: Request,
    api_token: str = Form(...),
    nome_fantasia: str = Form(...),
    prime_info: str = Form(""),
    prime_vendas: str = Form(""),
):
    """Novo cliente. O CNPJ vem do próprio ERP (dono do token), nunca do formulário: não há como gravar um token
    no cadastro de outra empresa. Cliente que já existe não é sobrescrito (lá tem 'Trocar token')."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    token, nome = api_token.strip(), " ".join(nome_fantasia.split())[:120]
    dados = await estado_api.testar(token)
    if dados["estado"] != "ok":
        _avisar(request, "Não cadastrado. " + estado_api.explicar(dados), "ruim")
        return _voltar(novo=True)
    cnpj = dados["cnpj"]
    if any(t["cnpj"] == cnpj for t in list_tenants()):
        _avisar(request, "Esse cliente já está cadastrado. Para corrigir o token, use \"Trocar token\" no cadastro dele.", "ruim")
        return _voltar(cnpj)
    create_tenant(cnpj=cnpj, nome_fantasia=nome or dados["fantasia"] or dados["razao"] or cnpj, api_token=token)
    set_tenant_ativo(cnpj, ativo=bool(prime_info))
    set_vendas_liberado(cnpj, liberado=bool(prime_vendas))
    _avisar(request, "Cliente cadastrado (token conferido com o CNPJ no ERP).")
    return _voltar(cnpj)


@router.post("/tenants/{cnpj}/token")
async def admin_trocar_token(request: Request, cnpj: str, api_token: str = Form(...)):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    token = api_token.strip()
    teste = await estado_api.testar(token)
    dono = teste["cnpj"]
    if teste["estado"] != "ok":
        _avisar(request, "Token não trocado (o anterior continua). " + estado_api.explicar(teste), "ruim")
    elif dono != normalize_cnpj(cnpj):
        _avisar(request, f"Token não trocado: ele é da empresa de CNPJ {_cnpj_formatado(dono)}, não deste cliente.", "ruim")
    else:
        set_tenant_token(cnpj, token)
        estado_api.esquecer(cnpj)
        _avisar(request, "Token trocado e conferido com o CNPJ no ERP.")
    return _voltar(cnpj)


@router.post("/tenants/{cnpj}/nome")
async def admin_editar_nome(request: Request, cnpj: str, nome_fantasia: str = Form(...)):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    nome = " ".join(nome_fantasia.split())[:120]
    if nome:
        set_tenant_nome(cnpj, nome)
        _avisar(request, "Nome atualizado.")
    return _voltar(cnpj)


@router.post("/tenants/{cnpj}/desativar")
async def admin_desativar_tenant(request: Request, cnpj: str):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    set_tenant_ativo(cnpj, ativo=False)
    _avisar(request, "Prime Info desligado: o cliente não entra mais no app.")
    return _voltar(cnpj)


@router.post("/tenants/{cnpj}/reativar")
async def admin_reativar_tenant(request: Request, cnpj: str):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    set_tenant_ativo(cnpj, ativo=True)
    _avisar(request, "Prime Info ligado.")
    return _voltar(cnpj)


@router.post("/tenants/{cnpj}/vendas/{acao}")
async def admin_prime_vendas(request: Request, cnpj: str, acao: str):
    """Liga/desliga o Prime Vendas da empresa. Bloquear desconecta na hora os celulares dela."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    if acao in ("liberar", "bloquear"):
        set_vendas_liberado(cnpj, liberado=acao == "liberar")
        _avisar(request, "Prime Vendas ligado." if acao == "liberar" else "Prime Vendas desligado: os celulares da empresa foram desconectados.")
    return _voltar(cnpj)


def _arquivo_de_backup() -> Response:
    conteudo, nome = backup.gerar()
    return Response(conteudo, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"', "Cache-Control": "no-store"})


@router.get("/consumo", response_class=HTMLResponse)
async def admin_consumo(request: Request, dias: int = 1):
    """Quanto da cota do ERP cada empresa e cada tela gastou, e quanta espera as pessoas tiveram."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    nomes = {nome_do_balde(t["api_token"]): t["nome_fantasia"] for t in list_tenants()}
    dias = max(1, min(dias, 30))
    por_cliente = consumo.resumo(dias)
    linhas = sorted(({"nome": nomes.get(cod, f"(código {cod})"), **dados} for cod, dados in por_cliente.items()),
                    key=lambda c: (-c["esperas"], -c["chamadas"]))
    return templates.TemplateResponse(request, "admin_consumo.html", {"linhas": linhas, "dias": dias})


@router.get("/desempenho", response_class=HTMLResponse)
async def admin_desempenho(request: Request, dias: int = 7, lentos: int = 0):
    """Logins e pedidos lentos, um a um, com onde o tempo foi (fila da cota, ERP, passo)."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    nomes = {t["cnpj"]: t["nome_fantasia"] for t in list_tenants()}
    linhas = desempenho.listar(dias, bool(lentos))
    for l in linhas:
        l["empresa"] = nomes.get(l["cnpj"], l["cnpj"] or "-")
    return templates.TemplateResponse(request, "admin_desempenho.html", {"linhas": linhas, "dias": max(1, min(dias, 90)), "lentos": 1 if lentos else 0})


def _tela_acessos(request: Request, cnpj: str = "", email: str = "", resultado: dict | None = None, erro: str = ""):
    empresas = sorted(({"cnpj": t["cnpj"], "nome": t["nome_fantasia"]} for t in list_tenants()), key=lambda e: e["nome"].lower())
    return templates.TemplateResponse(request, "admin_acessos.html", {
        "empresas": empresas, "cnpj": normalize_cnpj(cnpj), "email": email, "resultado": resultado, "erro": erro,
        "aviso": request.session.pop("aviso", None), "revalidar_min": acessos.REVALIDAR_A_CADA // 60,
    })


@router.get("/acessos", response_class=HTMLResponse)
async def admin_acessos(request: Request, c: str = ""):
    """O que o app enxerga de um usuário (perfil, permissão do Dashboard no ERP contra o que o app guardou, abas) e o botão Atualizar agora."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return _tela_acessos(request, cnpj=c)


@router.post("/acessos", response_class=HTMLResponse)
async def admin_acessos_ver(request: Request, cnpj: str = Form(""), email: str = Form("")):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    tenant = get_tenant_by_cnpj(normalize_cnpj(cnpj))
    email = email.strip()
    if tenant is None or not tenant.get("api_token"):
        return _tela_acessos(request, cnpj, email, erro="Escolha uma empresa cadastrada.")
    if "@" not in email:
        return _tela_acessos(request, cnpj, email, erro="Digite o e-mail do usuário.")
    try:
        resultado = await acessos.diagnosticar(tenant, email)
    except Exception:
        logger.exception("diagnóstico de acessos falhou (empresa %s)", tenant["cnpj"])
        return _tela_acessos(request, cnpj, email, erro="Não foi possível consultar o ERP agora. Tente de novo em instantes.")
    return _tela_acessos(request, cnpj, email, resultado)


@router.post("/acessos/atualizar")
async def admin_acessos_atualizar(request: Request, cnpj: str = Form("")):
    """Esquece as permissões e os usuários guardados da empresa; as sessões abertas dela se reconferem na próxima chamada."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    tenant = get_tenant_by_cnpj(normalize_cnpj(cnpj))
    if tenant is not None:
        acessos.esquecer_empresa(tenant["cnpj"])
        _avisar(request, "Pronto: as permissões guardadas desta empresa foram esquecidas. Quem está com o app aberto é reconferido na próxima ação.")
    return RedirectResponse(f"/admin/acessos?c={normalize_cnpj(cnpj)}", status_code=303)


@router.get("/backup")
async def admin_baixar_backup(request: Request):
    """Botão "Baixar backup agora" do admin (o arquivo já sai criptografado; só abre com a chave do PC do dono)."""
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    try:
        return _arquivo_de_backup()
    except backup.BackupIndisponivel:
        _avisar(request, "Backup não configurado: falta a variável BACKUP_PUBLIC_KEY no servidor.", "ruim")
        return _voltar()


@router.get("/backup/automatico")
async def backup_automatico(request: Request):
    """Download feito pela tarefa agendada no PC do dono, com o token BACKUP_TOKEN (cabeçalho Authorization: Bearer).
    Desligado sem BACKUP_TOKEN e BACKUP_PUBLIC_KEY. Tentativas erradas bloqueiam o IP por um tempo."""
    esperado = (settings.backup_token or "").strip()
    if len(esperado) < 32 or not backup.configurado():
        return JSONResponse({"erro": "backup automático desligado"}, status_code=404)
    chave = f"backup:{limite.ip_do_cliente(request) or 'sem-ip'}"
    espera = limite.segundos_bloqueado(chave, limite.LIMITE_ADMIN)
    if espera:
        return JSONResponse({"erro": limite.mensagem_bloqueio(espera)}, status_code=429)
    recebido = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(recebido.encode(), esperado.encode()):
        limite.registrar_falha(chave)
        logger.warning("tentativa de backup automático com token errado")
        return JSONResponse({"erro": "não autorizado"}, status_code=401)
    limite.limpar(chave)
    return _arquivo_de_backup()
