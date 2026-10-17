from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import secrets

from app import backup, limite
from app.config import ADMIN_ATIVO, settings
from app.security import normalize_cnpj
from app.tenants import (
    create_tenant, dados_da_empresa_do_token, empresa_do_token, list_tenants, set_tenant_ativo, set_tenant_nome,
    set_tenant_token, set_vendas_liberado,
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


def _aparelhos(cnpj: str) -> list[dict] | None:
    """Celulares conectados ao Prime Vendas. None se a tabela do Prime Vendas ainda não existe neste banco."""
    try:
        from vendas import sessao
        return [{**a, "ultimo_uso": _data_br(a["ultimo_uso"]), "desde": _data_br(a["criado_em"])} for a in sessao.aparelhos_da_empresa(cnpj)]
    except Exception:
        logger.warning("celulares do Prime Vendas indisponíveis para o /admin")
        return None


async def _conferencia(cliente: dict, todos: list[dict]) -> tuple[str, str]:
    """O token do cliente aberto é da empresa do CNPJ? Está repetido em outro cadastro? (1 consulta ao ERP.)"""
    if not cliente["api_token"]:
        return "Token ilegível (chave de criptografia ausente ou diferente)", "ruim"
    repetido = [t["nome_fantasia"] for t in todos if t["api_token"] == cliente["api_token"] and t["cnpj"] != cliente["cnpj"]]
    dono = await empresa_do_token(cliente["api_token"])
    problemas = []
    if repetido:
        problemas.append("Token repetido com: " + ", ".join(repetido))
    if dono is None:
        problemas.append("Não foi possível conferir no ERP (token inválido ou ERP fora do ar)")
    elif dono != normalize_cnpj(cliente["cnpj"]):
        problemas.append(f"Token é da empresa de CNPJ {_cnpj_formatado(dono)}, não deste CNPJ")
    return ("; ".join(problemas), "ruim") if problemas else ("Confere com o CNPJ", "bom")


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
    A lista não consulta o ERP; só o cliente aberto tem o token conferido."""
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
        texto, classe = await _conferencia(aberto, todos)
        aberto = {**aberto, "conf_texto": texto, "conf_classe": classe,
                  "aparelhos": _aparelhos(aberto["cnpj"])}
    contexto = {"clientes": [{k: v for k, v in t.items() if k != "api_token"} for t in clientes],
                "aberto": {k: v for k, v in aberto.items() if k != "api_token"} if aberto else None,
                "novo": bool(novo) or not clientes, "aviso": request.session.pop("aviso", None),
                "backup_configurado": backup.configurado()}
    return templates.TemplateResponse(request, "admin.html", contexto)


@router.post("/tenants/consultar")
async def admin_consultar_token(request: Request, api_token: str = Form(...)):
    """Novo cliente: diz de qual empresa é o token (CNPJ e nome, segundo o próprio ERP) e se já está cadastrada."""
    if not _is_admin(request):
        return JSONResponse({"erro": "Sessão expirada. Entre de novo."}, status_code=401)
    dados = await dados_da_empresa_do_token(api_token.strip())
    if dados is None:
        return JSONResponse({"erro": "O ERP não reconheceu esse token. Confira se ele foi copiado inteiro."}, status_code=422)
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
    dados = await dados_da_empresa_do_token(token)
    if dados is None:
        _avisar(request, "Não cadastrado: o ERP não reconheceu esse token. Confira se ele foi copiado inteiro.", "ruim")
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
    dono = await empresa_do_token(token)
    if dono is None:
        _avisar(request, "Token não trocado: o ERP não reconheceu esse token. Confira se ele foi copiado inteiro.", "ruim")
    elif dono != normalize_cnpj(cnpj):
        _avisar(request, f"Token não trocado: ele é da empresa de CNPJ {_cnpj_formatado(dono)}, não deste cliente.", "ruim")
    else:
        set_tenant_token(cnpj, token)
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


@router.post("/tenants/{cnpj}/aparelhos/{id_aparelho}/desconectar")
async def admin_desconectar_aparelho(request: Request, cnpj: str, id_aparelho: int):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    from vendas import sessao
    if sessao.desconectar(normalize_cnpj(cnpj), id_aparelho):
        _avisar(request, "Celular desconectado. O vendedor precisa entrar de novo com o e-mail e a senha do ERP.")
    return _voltar(cnpj)


def _arquivo_de_backup() -> Response:
    conteudo, nome = backup.gerar()
    return Response(conteudo, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"', "Cache-Control": "no-store"})


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
