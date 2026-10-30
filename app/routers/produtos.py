import asyncio

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import acessos, compras, versao
from app.cache import cached
from app.deps import current_tenant, sem_permissao
from app.meuerp_client import MeuERPClient
from app.sqlquery import ConsultaIndisponivel
from app.tenants import ERRO_ERP_LENTO, token_confere_sem_travar

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MAX_RESULTADOS_LISTA = 20

# Validade de cada informação (o ERP limita 20 chamadas/min por cliente). Nome e código do produto quase
# não mudam; custo e preço mudam de vez em quando; o saldo muda a cada venda.
TTL_LOJAS = 3600
TTL_BUSCA = 120
TTL_PRODUTO = 600
TTL_CUSTO_PRECO = 300
TTL_SALDO = 60
TTL_ENTRADAS = 600


def _nao_nulo(valor) -> bool:
    return valor is not None  # falha (None) não deve ficar guardada


def _erro_erp(erro: httpx.HTTPError) -> JSONResponse:
    """ERP demorou (passou do tempo) ou não respondeu: mensagem clara em vez de erro 500 na tela."""
    if isinstance(erro, httpx.TimeoutException):
        return JSONResponse({"detail": ERRO_ERP_LENTO}, status_code=504)
    return JSONResponse({"detail": "Não foi possível consultar o ERP agora. Tente de novo em instantes."}, status_code=502)


async def _preco_venda(client: MeuERPClient, id_variacao: int) -> float | None:
    try:
        resultado = await client.get(f"/api/mercadoria/{id_variacao}/tabelas-preco/v1", params={"limit": 10})
    except httpx.HTTPError:
        return None
    ativas = [i for i in (resultado.get("items") or []) if i.get("ativo") == "S"]
    if not ativas:
        return None
    ativas.sort(key=lambda i: i.get("codigoTabela") or 0)
    return ativas[0].get("valPreco")


async def _estoque(client: MeuERPClient, id_variacao: int, id_loja: int) -> float | None:
    try:
        resultado = await client.get(
            f"/api/mercadoria/{id_variacao}/local-estoque/{id_loja}/estoque/v1"
        )
    except httpx.HTTPError:
        return None
    return resultado.get("qtdSaldo")


@router.get("/produtos", response_class=HTMLResponse)
async def produtos_page(request: Request):
    tenant = current_tenant(request)
    if tenant is None:
        return RedirectResponse("/login?next=/produtos", status_code=303)
    # Cadastro com token de outra empresa: encerra a sessão; no login a pessoa vê o motivo.
    if not await token_confere_sem_travar(tenant):
        request.session.clear()
        return RedirectResponse("/login?next=/produtos", status_code=303)
    # Abas e informações da ficha conforme o perfil do usuário no ERP (recalculado a cada abertura).
    liberado = await acessos.atualizar(request, tenant)
    return templates.TemplateResponse(
        request,
        "produtos.html",
        {"empresa_nome": tenant["nome_fantasia"], "versao": versao.VERSAO, "acessos": liberado},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/produtos/lojas")
async def api_lojas(request: Request):
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    client = MeuERPClient(tenant["api_token"])
    try:
        resultado = await cached(
            (tenant["cnpj"], "lojas"),
            TTL_LOJAS,
            lambda: client.get("/api/local-estoque/v1", params={"limit": 100}),
            obsoleto_ate=TTL_LOJAS,
        )
    except httpx.HTTPError as erro:
        return _erro_erp(erro)
    lojas = [
        {"codigo": item["codigo"], "descricao": item["descricao"]}
        for item in (resultado.get("items") or [])
        if item.get("ativo") == "S"
    ]
    return {"lojas": lojas}


@router.get("/api/produtos/buscar")
async def api_buscar(request: Request, q: str = Query(min_length=2), loja: int = Query(...)):
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    if not acessos.pode(request, "produtos", "compras"):
        return sem_permissao()

    client = MeuERPClient(tenant["api_token"])
    try:
        # A loja não entra na busca (só no saldo, no detalhe), então todas as lojas dividem o mesmo resultado.
        resultado = await cached(
            (tenant["cnpj"], "busca", q.strip().lower()),
            TTL_BUSCA,
            lambda: client.get("/api/mercadoria/v1", params={"filtro": q, "limit": MAX_RESULTADOS_LISTA}),
        )
    except httpx.HTTPError as erro:
        return _erro_erp(erro)

    itens = resultado.get("items") or []

    # Preço e estoque não entram aqui de propósito: buscá-los por item deixaria a
    # busca em uma única chamada por resultado (até MAX_RESULTADOS_LISTA x 2), o que
    # estoura rápido o limite de 20 req/min da API e deixa a busca lenta. Eles só
    # são consultados na tela de detalhe, ao tocar em um produto específico.
    produtos = [
        {
            "idVariacao": item["codigoMercadoriaVariacao"],
            "descricao": item.get("descricao"),
            "codigoBarras": item.get("codigoBarras"),
            # O "código interno" que a pessoa vê no ERP e digita na busca é o da variação do produto
            # (o `codigoMercadoria` é o do cadastro-pai e, em outra busca, aponta para OUTRO produto).
            "codigoInterno": item["codigoMercadoriaVariacao"],
            "unidade": item.get("embalagem"),
        }
        for item in itens
    ]
    return {"total": resultado.get("total"), "produtos": produtos}


@router.get("/api/produtos/{id_variacao}/detalhe")
async def api_detalhe(request: Request, id_variacao: int, loja: int = Query(...)):
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    if not acessos.pode(request, "produtos"):
        return sem_permissao()
    ver_custo, ver_estoque = acessos.pode(request, "custo"), acessos.pode(request, "estoque")

    client = MeuERPClient(tenant["api_token"])
    cnpj = tenant["cnpj"]
    try:
        mercadoria = await cached(
            (cnpj, "produto", id_variacao), TTL_PRODUTO, lambda: client.get(f"/api/mercadoria/v1/{id_variacao}")
        )
    except httpx.HTTPStatusError as erro:
        if erro.response.status_code < 500:   # o ERP recusou o código: como antes, "não encontrado"
            return JSONResponse({"detail": "produto não encontrado"}, status_code=404)
        return _erro_erp(erro)
    except httpx.HTTPError as erro:
        return _erro_erp(erro)

    async def custo() -> float | None:
        try:
            resultado = await client.get(f"/api/mercadoria/{id_variacao}/custo/v1")
            return resultado.get("valCusto")
        except httpx.HTTPError:
            return None

    async def nada():
        return None

    # Cada informação com a sua validade: reabrir o mesmo produto não refaz as 4 chamadas. Custo e estoque só são
    # consultados para quem o perfil deixa ver (nem chegam ao celular dos outros).
    custo_valor, preco, estoque = await asyncio.gather(
        cached((cnpj, "custo", id_variacao), TTL_CUSTO_PRECO, custo, guardar_se=_nao_nulo) if ver_custo else nada(),
        cached((cnpj, "preco", id_variacao), TTL_CUSTO_PRECO, lambda: _preco_venda(client, id_variacao), guardar_se=_nao_nulo),
        cached((cnpj, "saldo", id_variacao, loja), TTL_SALDO, lambda: _estoque(client, id_variacao, loja), guardar_se=_nao_nulo)
        if ver_estoque else nada(),
    )

    return {
        "descricao": mercadoria.get("descricao"),
        "codigoBarras": mercadoria.get("codigoBarras"),
        "codigoInterno": id_variacao,  # mesmo código da busca (variação do produto), não o do cadastro-pai
        "unidade": mercadoria.get("embalagem"),
        "ativo": mercadoria.get("ativo") == "S",
        "precoCusto": custo_valor,
        "precoVenda": preco,
        "estoque": estoque,
    }


@router.get("/api/produtos/{id_variacao}/entradas")
async def api_entradas(request: Request, id_variacao: int, limite: int = Query(10, ge=1, le=compras.MAX_ENTRADAS)):
    """Últimas notas de entrada (compras) do produto: fornecedor, nota, quantidade, custo e desconto."""
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    if not acessos.pode(request, "compras"):
        return sem_permissao()

    client = MeuERPClient(tenant["api_token"])
    cnpj = tenant["cnpj"]
    try:
        # O preço de venda atual serve para calcular a margem de cada entrada (compartilha o cache da ficha).
        itens, preco = await asyncio.gather(
            cached(
                (cnpj, "entradas", id_variacao, limite),
                TTL_ENTRADAS,
                lambda: compras.entradas_produto(client, id_variacao, limite),
            ),
            cached((cnpj, "preco", id_variacao), TTL_CUSTO_PRECO, lambda: _preco_venda(client, id_variacao), guardar_se=_nao_nulo),
        )
    except ConsultaIndisponivel:
        return JSONResponse({"detail": "Consulta indisponível no momento."}, status_code=502)
    return {"entradas": itens, "limite": limite, "precoVenda": preco or 0}


@router.get("/api/produtos/{id_variacao}/historico-preco")
async def api_historico_preco(request: Request, id_variacao: int):
    """Mudanças do preço de venda, com o primeiro nome de quem alterou (nunca o e-mail)."""
    tenant = current_tenant(request)
    if tenant is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    if not acessos.pode(request, "historico_preco"):
        return sem_permissao()

    client = MeuERPClient(tenant["api_token"])
    try:
        mudancas = await cached(
            (tenant["cnpj"], "historico-preco", id_variacao),
            TTL_ENTRADAS,
            lambda: compras.historico_preco(client, id_variacao),
        )
    except ConsultaIndisponivel:
        return JSONResponse({"detail": "Consulta indisponível no momento."}, status_code=502)
    return {"mudancas": mudancas}
