"""Servidor do Prime Vendas (serviço próprio no Railway: uvicorn vendas.main:app).

Entrega o app do vendedor (PWA que funciona sem internet) e a API que ele usa. Reaproveita do PrimeSys Info só
as peças compartilhadas: cadastro das empresas (banco), criptografia dos tokens, cliente do ERP e limite de
tentativas de login. Não tem tela de administração: isso fica no /admin do PrimeSys Info.
"""

from app import registro

registro.configurar()

import asyncio
import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from app import limite, meuerp_client
from app.config import COOKIE_SEGURO
from app.database import db_session
from app.security import normalize_cnpj
from app.tenants import authenticate, get_tenant_by_cnpj
from vendas import catalogo, clientes, config, pdf_pedido, pedidos, sessao, vendedores
from vendas.db import criar_tabelas

logger = logging.getLogger(__name__)

# Fatia da cota do ERP que este processo pode usar (o PrimeSys Info usa a dele). Ajustado antes de qualquer chamada.
meuerp_client.RATE_LIMIT_CAPACITY = config.COTA_ERP_POR_MINUTO
meuerp_client.RESERVA_PARA_INTERATIVAS = 4   # login e pedido nunca esperam pela sincronização em segundo plano

ESTATICOS = Path(__file__).parent / "static"


def _versao() -> str:
    """Muda a cada publicação do Vendas: no Railway vem do commit publicado; fora dele, de um resumo dos arquivos.
    O app compara com a versão da tela aberta e mostra a faixa "Nova versão disponível" (igual ao PrimeSys Info);
    o service worker usa o mesmo número para trocar a cópia guardada no celular. Só um hash curto (não revela nada)."""
    base = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT_SHA")
    if not base:
        resumo = hashlib.sha256()
        pasta = Path(__file__).parent
        for arquivo in sorted(pasta.rglob("*")):
            if arquivo.is_file() and arquivo.suffix in {".py", ".html", ".js", ".json", ".png"} and "__pycache__" not in arquivo.parts:
                resumo.update(str(arquivo.relative_to(pasta)).encode())
                resumo.update(arquivo.read_bytes())
        base = resumo.hexdigest()
    return hashlib.sha256(f"primevendas:{base}".encode()).hexdigest()[:12]


VERSAO = _versao()

async def _conferir_pedidos_sempre() -> None:
    """A cada 2 minutos, para as empresas com pedidos em andamento: situação no ERP, código da venda gerada e
    cancelamento do pedido cuja venda foi cancelada. Não depende de algum celular estar com o app aberto."""
    while True:
        await asyncio.sleep(120)
        try:
            for cnpj in pedidos.empresas_com_pedidos_em_andamento():
                tenant = get_tenant_by_cnpj(cnpj)
                if config.empresa_liberada(tenant):
                    await pedidos.conferir_situacoes(tenant)
            # Quem é cada vendedor e as permissões: renovados antes de vencer, para o login ser rápido.
            for cnpj in sessao.empresas_com_aparelhos():
                tenant = get_tenant_by_cnpj(cnpj)
                if config.empresa_liberada(tenant):
                    await vendedores.renovar_se_preciso(tenant)
        except Exception:
            logger.exception("falha na conferência automática dos pedidos")


@asynccontextmanager
async def _ciclo_de_vida(app_):
    tarefa = asyncio.create_task(_conferir_pedidos_sempre())
    yield
    tarefa.cancel()


app = FastAPI(title="Prime Vendas", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_ciclo_de_vida)
app.add_middleware(GZipMiddleware, minimum_size=1024)

_CABECALHOS_SEGURANCA = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'",
}


@app.middleware("http")
async def cabecalhos(request: Request, call_next):
    resposta = await call_next(request)
    for nome, valor in _CABECALHOS_SEGURANCA.items():
        resposta.headers.setdefault(nome, valor)
    if COOKIE_SEGURO:   # na hospedagem (HTTPS)
        resposta.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/api/"):   # dados de uma empresa: nunca guardados por navegador ou intermediários
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


criar_tabelas()
catalogo.aplicar_regra_do_catalogo()


@app.api_route("/saude", methods=["GET", "HEAD"])
async def saude():
    try:
        with db_session() as conn:
            conn.execute("SELECT 1 FROM pv_aparelhos LIMIT 1").fetchone()
    except Exception:
        return JSONResponse({"ok": False}, status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/versao")
async def versao():
    return JSONResponse({"versao": VERSAO}, headers={"Cache-Control": "no-store"})


class Entrada(BaseModel):
    cnpj: str = Field(max_length=32)
    email: str = Field(max_length=200)
    senha: str = Field(max_length=200)


def _erro(status: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"erro": mensagem}, status_code=status)


@app.post("/api/entrar")
async def entrar(dados: Entrada, request: Request):
    """Primeiro acesso do aparelho: valida e-mail e senha no ERP (nada é guardado), descobre o funcionário ligado
    ao usuário e devolve o token do aparelho + o que o app precisa saber (nome, comissão, permissões)."""
    inicio = time.monotonic()
    cnpj, email = normalize_cnpj(dados.cnpj), dados.email.strip().lower()
    chave_usuario = f"vendas:{cnpj}:{email}"
    ip = limite.ip_do_cliente(request)
    chave_ip = f"vendas-ip:{ip}" if ip else None
    espera = max(limite.segundos_bloqueado(chave_usuario, limite.LIMITE_POR_USUARIO),
                 limite.segundos_bloqueado(chave_ip, limite.LIMITE_POR_IP) if chave_ip else 0)
    if espera:
        return _erro(429, limite.mensagem_bloqueio(espera))
    if not cnpj or not email or not dados.senha:
        return _erro(400, "Informe CNPJ, e-mail e senha.")

    def falhou():
        limite.registrar_falha(chave_usuario)
        if chave_ip:
            limite.registrar_falha(chave_ip)

    if not config.empresa_liberada(get_tenant_by_cnpj(cnpj)):   # confere no banco, antes de gastar chamada no ERP
        falhou()
        return _erro(403, "O Prime Vendas não está liberado para esta empresa. Fale com a DFTECH.")

    tenant, erro = await authenticate(cnpj, email, dados.senha)
    if erro:
        falhou()
        return _erro(401, erro)
    limite.limpar(chave_usuario)

    try:
        vendedor = await vendedores.do_email(tenant, email)
    except Exception:
        logger.exception("falha ao identificar o vendedor (empresa %s)", cnpj)
        return _erro(503, "Não foi possível consultar o ERP agora. Tente novamente em instantes.")
    if vendedor is None:
        return _erro(403, "Seu usuário do ERP não está ligado a um funcionário ativo. "
                          "No ERP, informe o seu usuário no cadastro de funcionário.")
    permissoes = await vendedores.permissoes(tenant, vendedor["id_perfil"])
    token = sessao.criar(cnpj, vendedor, aparelho=request.headers.get("user-agent", ""))
    logger.info("aparelho conectado: empresa %s, funcionário %s, login em %.1fs", cnpj, vendedor["id_funcionario"], time.monotonic() - inicio)
    return {
        "token": token,
        "empresa": {"cnpj": cnpj, "nome": tenant["nome_fantasia"]},
        "vendedor": {"id": vendedor["id_funcionario"], "nome": vendedor["nome"], "primeiro_nome": vendedor["primeiro_nome"],
                     "comissao": vendedor["per_comissao"]},
        "permissoes": permissoes,
        "pin_offline_dias": config.PIN_OFFLINE_DIAS,
    }


@app.get("/api/eu")
async def eu(aparelho: dict = Depends(sessao.aparelho_atual)):
    """O app chama ao abrir com internet: confirma que o acesso continua valendo e traz as permissões atuais
    (perfil mudado no ERP vale sem sair e entrar). Vem do que está guardado; ERP fora, o app mantém as que tem."""
    resposta = {"ok": True, "empresa": {"cnpj": aparelho["cnpj"], "nome": aparelho["tenant"]["nome_fantasia"]},
                "vendedor": {"id": aparelho["id_funcionario"], "nome": aparelho["nome_vendedor"]},
                "pin_offline_dias": config.PIN_OFFLINE_DIAS}
    try:
        vendedor = await vendedores.do_funcionario(aparelho["tenant"], aparelho["id_funcionario"])
        if vendedor:
            resposta["permissoes"] = await vendedores.permissoes(aparelho["tenant"], vendedor.get("id_perfil"))
    except Exception:
        logger.warning("permissões atuais indisponíveis para o aparelho; o app mantém as do login")
    return resposta


@app.post("/api/sair")
async def sair(request: Request, aparelho: dict = Depends(sessao.aparelho_atual)):
    sessao.revogar(request.headers.get("authorization", "").removeprefix("Bearer ").strip())
    return {"ok": True}


# ---- Catálogo no celular ----------------------------------------------------------------------------------------
@app.post("/api/sincronizar")
async def sincronizar(agora: int = 0, aparelho: dict = Depends(sessao.aparelho_atual)):
    """Pede ao servidor para atualizar a cópia do catálogo (lê do ERP só o que estiver vencido) e diz como está.
    agora=1: o vendedor tocou em Atualizar; lê as mudanças do ERP na hora (no máximo 1 vez por minuto por empresa)."""
    catalogo.disparar(aparelho["tenant"], forcar=bool(agora))
    return catalogo.estado(aparelho["cnpj"])


@app.get("/api/sincronizar/estado")
async def sincronizar_estado(aparelho: dict = Depends(sessao.aparelho_atual)):
    return catalogo.estado(aparelho["cnpj"])


@app.get("/api/dados/{tabela}")
async def dados(tabela: str, cursor: str = "", aparelho: dict = Depends(sessao.aparelho_atual)):
    """O que mudou na cópia depois do cursor que o celular guardou (página de até 2000 linhas)."""
    if tabela not in catalogo.TABELAS or len(cursor) > 80:
        return _erro(404, "Tabela desconhecida.")
    return catalogo.mudancas(aparelho["cnpj"], tabela, cursor)


# ---- Pedidos ----------------------------------------------------------------------------------------------------
@app.get("/api/produtos/{id_produto}/estoque")
async def produto_estoque(id_produto: int, aparelho: dict = Depends(sessao.aparelho_atual)):
    """Estoque do produto agora, em todos os locais (botão "Ver estoque agora" da ficha)."""
    try:
        locais = await catalogo.estoque_agora(aparelho["tenant"], id_produto)
    except Exception:
        logger.exception("estoque agora indisponível (empresa %s, produto %s)", aparelho["cnpj"], id_produto)
        return _erro(503, "ERP indisponível agora. Tente de novo em instantes.")
    if locais is None:
        return _erro(404, "Produto não encontrado.")
    return {"locais": locais}


@app.get("/api/pedido/config")
async def pedido_config(aparelho: dict = Depends(sessao.aparelho_atual)):
    """Formas de pagamento permitidas em cada tipo de documento (o celular guarda para montar pedidos sem internet)."""
    try:
        return await pedidos.config_da_empresa(aparelho["tenant"])
    except Exception:
        logger.exception("configuração de pedidos indisponível (empresa %s)", aparelho["cnpj"])
        return _erro(503, "ERP indisponível agora.")


@app.post("/api/pedidos")
async def pedido_enviar(request: Request, aparelho: dict = Depends(sessao.aparelho_atual)):
    try:
        dados = await request.json()
    except Exception:
        return _erro(400, "Pedido inválido.")
    if not isinstance(dados, dict) or len(await request.body()) > 200_000:
        return _erro(400, "Pedido inválido.")
    try:
        return await pedidos.receber(aparelho, dados)
    except pedidos.PedidoRecusado as erro:
        return _erro(422, str(erro))
    except pedidos.ErpIndisponivel:
        return _erro(503, "ERP indisponível agora. O pedido fica guardado e é enviado depois.")


@app.post("/api/pedidos/{id_pedido}/cancelar")
async def pedido_cancelar(id_pedido: str, aparelho: dict = Depends(sessao.aparelho_atual)):
    """Cancela um pedido do próprio vendedor que ainda não foi finalizado."""
    if len(id_pedido) > 40:
        return _erro(400, "Pedido inválido.")
    try:
        return await pedidos.cancelar(aparelho, id_pedido)
    except pedidos.PedidoRecusado as erro:
        return _erro(422, str(erro))
    except pedidos.ErpIndisponivel:
        return _erro(503, "ERP indisponível agora. Tente de novo em instantes.")


@app.get("/api/cep/{cep}")
async def consulta_cep(cep: str, aparelho: dict = Depends(sessao.aparelho_atual)):
    """Endereço pelo CEP (ViaCEP, chamado pelo servidor)."""
    try:
        achado = await clientes.buscar_cep(aparelho["cnpj"], cep)
    except clientes.CadastroRecusado as erro:
        return _erro(422, str(erro))
    except clientes.ServicoIndisponivel:
        return _erro(503, "Consulta de CEP indisponível agora. Digite o endereço.")
    return achado or _erro(404, "CEP não encontrado.")


@app.get("/api/cnpj/{cnpj}")
async def consulta_cnpj(cnpj: str, aparelho: dict = Depends(sessao.aparelho_atual)):
    """Já existe no ERP? + dados públicos da Receita (BrasilAPI). Só CNPJ: CPF nunca vai a serviço externo."""
    try:
        return await clientes.buscar_cnpj(aparelho, cnpj)
    except clientes.CadastroRecusado as erro:
        return _erro(422, str(erro))
    except clientes.ServicoIndisponivel:
        return _erro(503, "ERP indisponível agora. Tente de novo.")


@app.post("/api/clientes")
async def cliente_cadastrar(request: Request, aparelho: dict = Depends(sessao.aparelho_atual)):
    try:
        dados = await request.json()
    except Exception:
        return _erro(400, "Cadastro inválido.")
    if not isinstance(dados, dict) or len(await request.body()) > 20_000:
        return _erro(400, "Cadastro inválido.")
    try:
        return await clientes.cadastrar(aparelho, dados)
    except clientes.CadastroRecusado as erro:
        return _erro(422, str(erro))
    except clientes.ServicoIndisponivel:
        return _erro(503, "ERP indisponível agora. O cadastro fica guardado e é enviado depois.")


@app.get("/api/pedidos/{id_pedido}/pdf")
async def pedido_pdf(id_pedido: str, aparelho: dict = Depends(sessao.aparelho_atual)):
    """PDF do pedido (logo e dados da loja), para o vendedor mandar ao cliente pelo WhatsApp ou outro app."""
    if len(id_pedido) > 40:
        return _erro(400, "Pedido inválido.")
    try:
        conteudo, nome = await pdf_pedido.gerar(aparelho, id_pedido)
    except pedidos.PedidoRecusado as erro:
        return _erro(422, str(erro))
    except Exception:
        logger.exception("falha gerando o PDF do pedido")
        return _erro(503, "Não foi possível gerar o PDF agora. Tente de novo.")
    return Response(conteudo, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{nome}"', "Cache-Control": "no-store"})


@app.get("/api/pedidos")
async def pedido_lista(cursor: str = "", aparelho: dict = Depends(sessao.aparelho_atual)):
    """Situação dos pedidos do vendedor (no caixa, finalizado, faturado...). Confere o ERP no máximo a cada 2 min."""
    if len(cursor) > 120:
        return _erro(400, "Cursor inválido.")
    try:
        await pedidos.conferir_situacoes(aparelho["tenant"])
    except Exception:
        logger.exception("falha conferindo situação dos pedidos")
    return pedidos.do_vendedor(aparelho["cnpj"], aparelho["id_funcionario"], cursor)


# ---- App (PWA) -------------------------------------------------------------------------------------------------
@app.get("/privacidade")
async def privacidade():
    """A política de privacidade é uma só para os dois apps (publicada no Prime Info)."""
    return RedirectResponse(config.URL_PRIVACIDADE, status_code=302)


@app.get("/sw.js")
async def service_worker():
    # Na raiz para controlar o app inteiro; nunca em cache, senão o celular não vê atualização.
    corpo = (ESTATICOS / "sw.js").read_text(encoding="utf-8").replace("__VERSAO__", VERSAO)
    return Response(corpo, media_type="text/javascript", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/.well-known/assetlinks.json")
async def assetlinks():
    """O Android confere este arquivo para abrir o APK do Prime Vendas como app (sem barra de navegador).
    Gerado junto com o APK (PWABuilder); só tem o nome do pacote e a impressão digital pública da assinatura."""
    arquivo = ESTATICOS / "well-known" / "assetlinks.json"
    if not arquivo.is_file():
        return JSONResponse([], headers={"Cache-Control": "no-cache"})
    return FileResponse(arquivo, media_type="application/json", headers={"Cache-Control": "no-cache"})


@app.get("/")
async def inicio():
    # A tela leva o número da versão para saber quando há uma nova publicação.
    pagina = (ESTATICOS / "index.html").read_text(encoding="utf-8").replace("__VERSAO__", VERSAO)
    return Response(pagina, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")
