"""Pedidos do Prime Vendas: o celular monta o pedido (mesmo sem internet) e envia; o servidor confere tudo de novo
e lança no ERP.

  - Pedido de venda (PA): natureza "PEDIDO VENDA", status E -> o caixa importa no PDV ou a loja finaliza na nuvem;
    na finalização é que se escolhe pré-venda (não fiscal) ou NFC-e (fiscal).
  - Pré-venda (PV) como tipo de documento saiu do app (decisão de 13/10/2026): o pedido de venda já cobre os dois
    caminhos. O código continua aceitando ler pré-vendas antigas, mas não cria novas (TIPOS_ATIVOS).
Nada vindo do celular é aceito sem conferência: cliente e produtos têm que existir na cópia do catálogo, o preço é
o do servidor, desconto respeita o produto e o perfil do vendedor, a comissão vem do cadastro do funcionário e as
formas de pagamento têm que ser as permitidas para aquele tipo de documento (configuradas na natureza, no ERP).
Cada pedido tem um identificador criado no celular (idExterno no ERP): reenviar o mesmo pedido nunca duplica.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from app.cache import cached
from app.database import db_session
from app.meuerp_client import MeuERPClient
from app.sqlquery import executar, i
from vendas import vendedores
from vendas.db import agora_texto, carimbo_agora, texto_para_data

logger = logging.getLogger(__name__)

TIPOS = {
    "PA": {"nome": "Pedido de venda", "natureza_padrao": 530, "descricao": "PEDIDO VENDA", "status": "E"},
    "PV": {"nome": "Pré-venda", "natureza_padrao": 500, "descricao": "PRE-VENDA", "status": "R"},
}
TIPOS_ATIVOS = ("PA",)
TTL_CONFIG = 6 * 3600
FUSO_LOJA = timezone(timedelta(hours=-3))   # horário de Brasília (sem horário de verão desde 2019)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DATA_HORA = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


class PedidoRecusado(Exception):
    """Problema no pedido em si (o vendedor precisa corrigir). Reenviar igual não adianta."""


class ErpIndisponivel(Exception):
    """ERP fora do ar / limite de chamadas: o celular tenta de novo depois."""


# ---- Configuração por empresa: natureza e formas de pagamento de cada tipo de documento ---------------------------
async def _ler_config(token: str) -> dict:
    client = MeuERPClient(token)
    naturezas = (await client.get("/api/natureza-operacao/v1", params={"limit": 100})).get("items") or []
    pagamentos = {int(p["codigo"]): p for p in (await client.get("/api/pagamento/v1", params={"limit": 100})).get("items") or []
                  if p.get("ativo") == "S"}
    parcelamentos: dict[int, list] = {}
    config = {}
    for tipo in TIPOS_ATIVOS:
        regra = TIPOS[tipo]
        ativas = [n for n in naturezas if n.get("ativo") == "S" and n.get("tipoMovimento") == "S"]
        natureza = next((n for n in ativas if int(n["codigo"]) == regra["natureza_padrao"]), None) \
            or next((n for n in ativas if (n.get("descricao") or "").strip().upper() == regra["descricao"]), None)
        if natureza is None:
            config[tipo] = {"nome": regra["nome"], "disponivel": False, "pagamentos": []}
            continue
        permitidos = (await client.get(f"/api/natureza-operacao/{int(natureza['codigo'])}/pagamentos/v1", params={"limit": 100})).get("items") or []
        lista = []
        for item in permitidos:
            pid = int(item["idPagamento"])
            if item.get("ativo") != "S" or pid not in pagamentos:
                continue
            p = pagamentos[pid]
            a_vista = p.get("aVista") == "S"
            if not a_vista and pid not in parcelamentos:
                itens = (await client.get(f"/api/pagamento/{pid}/parcelamentos/v1", params={"limit": 100})).get("items") or []
                parcelamentos[pid] = [{"id": int(x["idParcelamento"]), "descricao": x.get("descricao") or "",
                                       "parcelas": max(1, int(x.get("qtdParcelas") or 1)), "dias_primeira": int(x.get("qtdDiasPrimeira") or 0),
                                       "intervalo": int(x.get("qtdDiasIntervalo") or 30), "entrada": x.get("entrada") == "S"}
                                      for x in itens if x.get("ativo") == "S"]
            lista.append({"id": pid, "descricao": (p.get("descricao") or "").strip().title() or p.get("tipoDescricao"),
                          "tipo": p.get("tipoDescricao") or "", "a_vista": a_vista, "parcelamentos": [] if a_vista else parcelamentos[pid]})
        config[tipo] = {"nome": regra["nome"], "disponivel": True, "natureza": int(natureza["codigo"]), "pagamentos": lista}
    return config


async def config_da_empresa(tenant: dict) -> dict:
    return await cached((tenant["cnpj"], "vendas-config-pedido"), TTL_CONFIG, lambda: _ler_config(tenant["api_token"]),
                        obsoleto_ate=24 * 3600)


# ---- Conferência do pedido -----------------------------------------------------------------------------------------
def _num(v, nome: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise PedidoRecusado(f"Valor inválido em {nome}.")
    if x != x or abs(x) > 1e9:   # NaN ou absurdo
        raise PedidoRecusado(f"Valor inválido em {nome}.")
    return x


def _data_hora_do_pedido(texto: str) -> str:
    """Hora em que o vendedor fez o pedido (vale mesmo se ficou na fila sem internet). Se vier estranha, usa agora."""
    agora = datetime.now(FUSO_LOJA).replace(tzinfo=None, microsecond=0)
    if isinstance(texto, str) and _DATA_HORA.match(texto):
        quando = datetime.fromisoformat(texto)
        if agora - timedelta(days=30) <= quando <= agora + timedelta(hours=2):
            return quando.isoformat()
    return agora.isoformat()


async def conferir(aparelho: dict, dados: dict) -> dict:
    """Confere o pedido do celular contra a cópia do catálogo, o cadastro do vendedor e as regras do ERP.
    Devolve o corpo pronto para o ERP + o resumo para gravar. Levanta PedidoRecusado com a explicação."""
    tenant, cnpj = aparelho["tenant"], aparelho["cnpj"]
    tipo = dados.get("tipo")
    if tipo not in TIPOS_ATIVOS:
        raise PedidoRecusado("O app envia só pedido de venda. Refaça como pedido de venda.")
    config = (await config_da_empresa(tenant))[tipo]
    if not config.get("disponivel"):
        raise PedidoRecusado(f"A natureza de operação de {TIPOS[tipo]['nome']} não foi encontrada no ERP.")

    vendedor = await vendedores.do_funcionario(tenant, aparelho["id_funcionario"])
    if vendedor is None:
        raise PedidoRecusado("Seu cadastro de vendedor não está mais ativo no ERP.")
    id_usuario = int(aparelho["id_usuario"] or vendedor.get("id_usuario") or 0)
    if not id_usuario:
        raise PedidoRecusado("Para enviar pedidos, o funcionário precisa ter um usuário do ERP vinculado no cadastro.")
    permissoes = await vendedores.permissoes(tenant, vendedor.get("id_perfil"))

    try:
        id_cliente = int(dados.get("cliente"))
    except (TypeError, ValueError):
        raise PedidoRecusado("Escolha o cliente.")
    with db_session() as conn:
        cliente = conn.execute("SELECT id, nome FROM pv_clientes WHERE cnpj = ? AND id = ? AND ativo = 1", (cnpj, id_cliente)).fetchone()
    if cliente is None:
        raise PedidoRecusado("Cliente não encontrado ou inativo. Atualize os dados do app.")

    itens_celular = dados.get("itens") or []
    if not isinstance(itens_celular, list) or not 1 <= len(itens_celular) <= 300:
        raise PedidoRecusado("O pedido precisa ter de 1 a 300 itens.")
    ids = [int(_num(x.get("produto"), "produto")) for x in itens_celular]
    with db_session() as conn:
        marcas = ",".join("?" * len(set(ids)))
        produtos = {int(r["id"]): dict(r) for r in conn.execute(
            f"SELECT id, descricao, preco, permite_desconto, desconto_max, ativo FROM pv_produtos WHERE cnpj = ? AND id IN ({marcas})",
            (cnpj, *set(ids)))}

    itens_erp, total_bruto, total_desconto = [], 0.0, 0.0
    for x, pid in zip(itens_celular, ids):
        produto = produtos.get(pid)
        if produto is None or not produto["ativo"]:
            raise PedidoRecusado(f"Produto {pid} não existe mais ou está inativo.")
        if produto["preco"] is None or produto["preco"] <= 0:
            raise PedidoRecusado(f"{produto['descricao']} está sem preço no ERP.")
        qtd = _num(x.get("qtd"), "quantidade")
        if qtd <= 0:
            raise PedidoRecusado(f"Quantidade inválida em {produto['descricao']}.")
        preco = round(float(produto["preco"]), 2)
        if abs(_num(x.get("preco"), "preço") - preco) > 0.005:
            raise PedidoRecusado(f"O preço de {produto['descricao']} mudou para R$ {preco:.2f}. Revise o pedido.")
        bruto = round(preco * qtd, 2)
        desconto = round(_num(x.get("desconto") or 0, "desconto"), 2)
        if desconto < 0 or desconto >= bruto:
            raise PedidoRecusado(f"Desconto inválido em {produto['descricao']}.")
        if desconto > 0:
            if not produto["permite_desconto"]:
                raise PedidoRecusado(f"{produto['descricao']} não permite desconto.")
            if not permissoes.get("desconto_item"):
                raise PedidoRecusado("Seu perfil no ERP não permite dar desconto.")
            limite = produto["desconto_max"]
            if limite and desconto / bruto * 100 > limite + 0.01 and not permissoes.get("desconto_acima_do_limite"):
                raise PedidoRecusado(f"Desconto acima do máximo de {limite:g}% em {produto['descricao']}.")
        total_bruto += bruto
        total_desconto += desconto
        itens_erp.append({"idMercadoriaVariacao": pid, "valUnitarioBruto": preco, "qtd": qtd, "valDesconto": desconto,
                          "idPessoaFuncionario": int(aparelho["id_funcionario"]), "perComissao": vendedor["per_comissao"]})
    total = round(total_bruto - total_desconto, 2)

    permitidos = {p["id"]: p for p in config["pagamentos"]}
    pagamentos_celular = dados.get("pagamentos") or []
    if not isinstance(pagamentos_celular, list) or not 1 <= len(pagamentos_celular) <= 10:
        raise PedidoRecusado("Informe a forma de pagamento.")
    pagamentos_erp, parcelas_erp, soma = [], [], 0.0
    base = date.fromisoformat(_data_hora_do_pedido(dados.get("criado_em"))[:10])
    for seq, pg in enumerate(pagamentos_celular, start=1):
        forma = permitidos.get(int(_num(pg.get("forma"), "forma de pagamento")))
        if forma is None:
            raise PedidoRecusado(f"Forma de pagamento não permitida para {TIPOS[tipo]['nome']}.")
        valor = round(_num(pg.get("valor"), "valor do pagamento"), 2)
        if valor <= 0:
            raise PedidoRecusado("Valor de pagamento inválido.")
        soma += valor
        pagamentos_erp.append({"idPagamento": forma["id"], "valor": valor})
        # O ERP exige ao menos uma parcela para cada forma de pagamento. À vista: uma parcela com vencimento no dia.
        # (Pedido de venda não movimenta o financeiro; pré-venda só entra no "a receber" depois de faturada.)
        if forma["a_vista"]:
            parcelas_erp.append({"idSequencia": seq, "idParcela": 1, "valBase": valor, "valParcela": valor,
                                 "dtVencimento": base.isoformat() + "T00:00:00"})
        else:
            opcoes = {p["id"]: p for p in forma["parcelamentos"]}
            plano = opcoes.get(int(_num(pg.get("parcelamento") or 0, "parcelamento"))) or (forma["parcelamentos"] or [None])[0]
            n = plano["parcelas"] if plano else 1
            primeira = plano["dias_primeira"] if plano else 30
            intervalo = plano["intervalo"] if plano else 30
            cada = round(valor / n, 2)
            for k in range(n):
                vp = round(valor - cada * (n - 1), 2) if k == n - 1 else cada
                parcelas_erp.append({"idSequencia": seq, "idParcela": k + 1, "valBase": vp, "valParcela": vp,
                                     "dtVencimento": (base + timedelta(days=primeira + intervalo * k)).isoformat() + "T00:00:00"})
    if abs(round(soma, 2) - total) > 0.01:
        raise PedidoRecusado(f"A soma dos pagamentos (R$ {soma:.2f}) é diferente do total do pedido (R$ {total:.2f}).")

    observacao = str(dados.get("observacao") or "")[:500]
    data_hora = _data_hora_do_pedido(dados.get("criado_em"))
    corpo = {
        "documento": {"idCaixa": 0, "idAbertura": 0, "idUsuario": id_usuario, "idPessoa": id_cliente, "dataHora": data_hora,
                      "modelo": tipo, "serie": "0",   # mesma série dos pedidos de venda feitos na nuvem
                      "idNaturezaOperacao": config["natureza"], "idExterno": dados["id"],
                      "status": TIPOS[tipo]["status"], "observacao": observacao},
        "pessoa": {"idPessoa": id_cliente},
        "mercadoriaLista": itens_erp,
        "pagamentoLista": pagamentos_erp,
    }
    if parcelas_erp:
        corpo["parcelaLista"] = parcelas_erp
    if tipo == "PA":
        corpo["pedido"] = {"tipoEntrega": "B", "dtEntrega": data_hora, "idExterno": dados["id"]}
    resumo = {"tipo": tipo, "cliente": id_cliente, "cliente_nome": cliente["nome"], "total": total, "desconto": round(total_desconto, 2),
              "itens": len(itens_erp), "criado_em": data_hora}
    return {"corpo": corpo, "resumo": resumo}


# ---- Envio ao ERP (sem duplicar) ------------------------------------------------------------------------------------
def _gravar(id_pedido: str, **campos) -> None:
    campos["carimbo"] = carimbo_agora()
    with db_session() as conn:
        for coluna, valor in campos.items():   # colunas fixas deste módulo
            conn.execute(f"UPDATE pv_pedidos SET {coluna} = ? WHERE id = ?", (valor, id_pedido))


def _linha(id_pedido: str) -> dict | None:
    with db_session() as conn:
        r = conn.execute("SELECT * FROM pv_pedidos WHERE id = ?", (id_pedido,)).fetchone()
    return dict(r) if r else None


async def _ja_existe_no_erp(client: MeuERPClient, id_pedido: str) -> int | None:
    """O ERP não impede dois documentos com o mesmo idExterno: antes de reenviar, procura se já entrou."""
    linhas = await executar(client, f"select _iddocumento as id from documento where idexterno = '{id_pedido}' order by 1 limit 1", max_paginas=1)
    return int(linhas[0]["id"]) if linhas else None


async def receber(aparelho: dict, dados: dict) -> dict:
    """Recebe um pedido do celular. Devolve a situação: enviado (com o código do documento no ERP) ou recusado."""
    id_pedido = str(dados.get("id") or "").lower()
    if not _UUID.match(id_pedido):
        raise PedidoRecusado("Identificador do pedido inválido.")
    dados["id"] = id_pedido
    existente = _linha(id_pedido)
    if existente and existente["cnpj"] != aparelho["cnpj"]:
        raise PedidoRecusado("Identificador do pedido inválido.")
    if existente and existente["id_documento"]:
        return _para_celular(existente)

    conferido = await conferir(aparelho, dados)
    if existente is None:
        agora = agora_texto()
        with db_session() as conn:
            conn.execute(
                """INSERT INTO pv_pedidos (id, cnpj, id_aparelho, id_funcionario, tipo, dados, resumo, situacao, mensagem,
                                           criado_em, atualizado_em, carimbo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'enviando', '', ?, ?, ?) ON CONFLICT (id) DO NOTHING""",
                (id_pedido, aparelho["cnpj"], aparelho["id"], aparelho["id_funcionario"], conferido["resumo"]["tipo"],
                 json.dumps(dados, ensure_ascii=False), json.dumps(conferido["resumo"], ensure_ascii=False), agora, agora, carimbo_agora()))

    client = MeuERPClient(aparelho["tenant"]["api_token"])
    try:
        id_documento = await _ja_existe_no_erp(client, id_pedido)
        if id_documento is None:
            resposta = await client.post("/api/documento/v1", json=conferido["corpo"])
            if not resposta.get("status") or not resposta.get("idDocumento"):
                mensagem = resposta.get("mensagem") or "O ERP recusou o pedido."
                _gravar(id_pedido, situacao="recusado", mensagem=mensagem[:300], atualizado_em=agora_texto())
                logger.warning("ERP recusou pedido %s (empresa %s): %s", id_pedido, aparelho["cnpj"], mensagem)
                return _para_celular(_linha(id_pedido))
            id_documento = int(resposta["idDocumento"])
    except httpx.HTTPStatusError as erro:
        if erro.response.status_code in (400, 404, 409, 422):
            try:
                mensagem = erro.response.json().get("mensagem") or erro.response.text
            except Exception:
                mensagem = erro.response.text
            _gravar(id_pedido, situacao="recusado", mensagem=str(mensagem)[:300], atualizado_em=agora_texto())
            return _para_celular(_linha(id_pedido))
        raise ErpIndisponivel() from erro
    except (httpx.HTTPError, RuntimeError) as erro:
        raise ErpIndisponivel() from erro

    situacao = "rascunho" if conferido["resumo"]["tipo"] == "PV" else "no_caixa"
    _gravar(id_pedido, id_documento=id_documento, situacao=situacao, mensagem="", enviado_em=agora_texto(), atualizado_em=agora_texto())
    logger.info("pedido %s enviado ao ERP: documento %s (empresa %s)", id_pedido, id_documento, aparelho["cnpj"])
    return _para_celular(_linha(id_pedido))


def _para_celular(linha: dict) -> dict:
    return {"id": linha["id"], "tipo": linha["tipo"], "documento": linha["id_documento"], "situacao": linha["situacao"],
            "mensagem": linha["mensagem"], "vendas": linha.get("vendas") or "", "resumo": json.loads(linha["resumo"] or "{}"), "dados": json.loads(linha["dados"] or "{}")}


# ---- Situação no ERP (no caixa, finalizado, faturado, cancelado) -----------------------------------------------------
_ultima_conferencia: dict[str, datetime] = {}
CONFERIR_A_CADA = timedelta(minutes=2)


def _situacao(tipo: str, status: str, importado: str) -> str:
    if status in ("C", "X"):
        return "cancelado"
    if tipo == "PA":
        return "finalizado" if importado == "T" else "no_caixa"
    return "faturado" if status == "E" else "rascunho"


_ORIGEM = re.compile(r"Docs Origem:\s*([\d,\s]+)", re.I)


async def _cancelar_no_erp(client: MeuERPClient, id_documento: int) -> None:
    """A nuvem não deixa cancelar documento criado pela API ("de outra abertura"); pela API pode."""
    resposta = await client.post(f"/api/documento/{int(id_documento)}/status/v1", json={"status": "C"})
    if not resposta.get("status"):
        raise PedidoRecusado(resposta.get("mensagem") or "O ERP não aceitou o cancelamento.")


async def conferir_situacoes(tenant: dict) -> None:
    """Uma rodada de conferência para todos os pedidos da empresa (2 consultas, não 1 por pedido):
      - situação no ERP (aguardando finalizar, finalizado, cancelado);
      - pedido finalizado cuja venda gerada ("Docs Origem: nº") foi cancelada -> cancela o pedido também, pela API,
        para não ficar um pedido "Emitido" sobrando (a nuvem não consegue cancelar pedido criado pelo app)."""
    cnpj = tenant["cnpj"]
    agora = datetime.now(timezone.utc)
    if agora - _ultima_conferencia.get(cnpj, datetime.min.replace(tzinfo=timezone.utc)) < CONFERIR_A_CADA:
        return
    _ultima_conferencia[cnpj] = agora
    limite = (agora - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as conn:
        abertos = {int(r["id_documento"]): dict(r) for r in conn.execute(
            "SELECT id, tipo, id_documento, situacao, vendas FROM pv_pedidos WHERE cnpj = ? AND id_documento IS NOT NULL "
            "AND situacao IN ('no_caixa', 'rascunho', 'finalizado') AND criado_em >= ?", (cnpj, limite))}
    if not abertos:
        return
    ids = sorted(abertos)[-1000:]
    lista = ",".join(i(x) for x in ids)
    client = MeuERPClient(tenant["api_token"], segundo_plano=True)
    try:
        linhas = await executar(client, f"select _iddocumento as id, status, coalesce(flagimportado, 'F') as importado "
                                        f"from documento where _iddocumento in ({lista})", max_paginas=10)
        # Vendas geradas a partir dos pedidos (o ERP grava "Docs Origem: <nº do pedido>" na observação da venda).
        vendas = await executar(client, f"select _iddocumento as id, status, observacao from documento "
                                         f"where _iddocumento > {i(ids[0])} and observacao like 'Docs Origem:%'", max_paginas=10)
    except Exception:
        logger.warning("não foi possível conferir a situação dos pedidos da empresa %s", cnpj)
        return
    vendas_do_pedido: dict[int, list[tuple[int, str]]] = {}
    for v in vendas:
        achado = _ORIGEM.search(v.get("observacao") or "")
        for origem in re.findall(r"\d+", achado.group(1)) if achado else []:
            vendas_do_pedido.setdefault(int(origem), []).append((int(v["id"]), v["status"]))

    for l in linhas:
        pedido = abertos.get(int(l["id"]))
        if pedido is None:
            continue
        nova = _situacao(pedido["tipo"], l["status"], l["importado"])
        geradas = sorted(vendas_do_pedido.get(int(l["id"]), []))
        situacoes_vendas = [st for _, st in geradas]
        # Código(s) da venda gerada: mostra as que valem; se todas foram canceladas, mostra mesmo assim (histórico).
        validas = [str(v) for v, st in geradas if st not in ("C", "X")] or [str(v) for v, _ in geradas]
        codigo_vendas = ",".join(validas)
        if nova == "finalizado" and situacoes_vendas and all(st in ("C", "X") for st in situacoes_vendas):
            try:
                await _cancelar_no_erp(client, int(l["id"]))
                nova = "cancelado"
                logger.info("pedido %s cancelado porque a venda gerada foi cancelada (empresa %s)", l["id"], cnpj)
            except Exception:
                logger.warning("não foi possível cancelar o pedido %s (venda já cancelada)", l["id"])
        mudancas = {}
        if nova != pedido["situacao"]:
            mudancas["situacao"] = nova
        if codigo_vendas and codigo_vendas != (pedido.get("vendas") or ""):
            mudancas["vendas"] = codigo_vendas
        if mudancas:
            _gravar(pedido["id"], atualizado_em=agora_texto(), **mudancas)


async def cancelar(aparelho: dict, id_pedido: str) -> dict:
    """Vendedor cancela um pedido seu que ainda não foi finalizado (o cliente desistiu)."""
    linha = _linha(str(id_pedido).lower())
    if linha is None or linha["cnpj"] != aparelho["cnpj"] or int(linha["id_funcionario"]) != int(aparelho["id_funcionario"]):
        raise PedidoRecusado("Pedido não encontrado.")
    if linha["situacao"] == "cancelado":
        return _para_celular(linha)
    if not linha["id_documento"]:   # recusado pelo ERP: nunca entrou lá
        _gravar(linha["id"], situacao="cancelado", atualizado_em=agora_texto())
        return _para_celular(_linha(linha["id"]))
    client = MeuERPClient(aparelho["tenant"]["api_token"])
    try:
        atual = await executar(client, f"select status, coalesce(flagimportado, 'F') as importado from documento "
                                       f"where _iddocumento = {i(linha['id_documento'])}", max_paginas=1)
        if not atual:
            raise PedidoRecusado("Pedido não encontrado no ERP.")
        situacao = _situacao(linha["tipo"], atual[0]["status"], atual[0]["importado"])
        if situacao == "cancelado":
            _gravar(linha["id"], situacao="cancelado", atualizado_em=agora_texto())
            return _para_celular(_linha(linha["id"]))
        if situacao == "finalizado":
            _gravar(linha["id"], situacao="finalizado", atualizado_em=agora_texto())
            raise PedidoRecusado("Este pedido já foi finalizado e virou venda. Cancele a venda no PDV ou na nuvem: "
                                 "o pedido é cancelado junto, automaticamente.")
        await _cancelar_no_erp(client, int(linha["id_documento"]))
    except PedidoRecusado:
        raise
    except Exception as erro:
        raise ErpIndisponivel() from erro
    _gravar(linha["id"], situacao="cancelado", atualizado_em=agora_texto())
    logger.info("pedido %s (documento %s) cancelado pelo vendedor (empresa %s)", linha["id"], linha["id_documento"], aparelho["cnpj"])
    return _para_celular(_linha(linha["id"]))


def empresas_com_pedidos_em_andamento() -> list[str]:
    """CNPJs com pedidos dos últimos 60 dias ainda acompanhados (aguardando finalizar ou finalizados)."""
    limite = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as conn:
        return [r["cnpj"] for r in conn.execute(
            "SELECT DISTINCT cnpj FROM pv_pedidos WHERE id_documento IS NOT NULL AND situacao IN ('no_caixa', 'finalizado') "
            "AND criado_em >= ?", (limite,))]


def do_vendedor(cnpj: str, id_funcionario: int, cursor: str, limite: int = 200) -> dict:
    """Pedidos do vendedor que mudaram depois do cursor ('carimbo|id'), para o celular atualizar a lista."""
    carimbo, _, ultimo = (cursor or "").partition("|")
    with db_session() as conn:
        linhas = [dict(r) for r in conn.execute(
            "SELECT * FROM pv_pedidos WHERE cnpj = ? AND id_funcionario = ? AND (carimbo > ? OR (carimbo = ? AND id > ?)) "
            "ORDER BY carimbo, id LIMIT ?", (cnpj, id_funcionario, carimbo, carimbo, ultimo, limite + 1))]
    mais = len(linhas) > limite
    linhas = linhas[:limite]
    novo = f"{linhas[-1]['carimbo']}|{linhas[-1]['id']}" if linhas else (cursor or "")
    return {"itens": [_para_celular(l) for l in linhas], "cursor": novo, "mais": mais}
