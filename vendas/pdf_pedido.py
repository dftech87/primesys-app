"""PDF do pedido de venda, para o vendedor mandar ao cliente (WhatsApp ou outro app do celular).

Gerado no servidor a partir do que já está gravado (pedido, catálogo e clientes): nenhuma consulta ao ERP por PDF.
Os dados da loja e a logo vêm de /api/empresa/v1 e ficam guardados 1 dia por empresa. A logo é reduzida antes de
guardar; se não vier ou não for uma imagem válida, o PDF sai sem logo.
"""

import asyncio
import base64
import io
import logging

from fpdf import FPDF
from fpdf.fonts import FontFace
from PIL import Image

from app.database import db_session
from app.meuerp_client import MeuERPClient
from vendas import pedidos
from vendas.memoria import guardado

logger = logging.getLogger(__name__)

TTL_EMPRESA = 24 * 3600
LOGO_MAX_PX = 400
LOGO_MAX_BYTES = 3_000_000
AZUL = (30, 64, 175)
CINZA = (100, 116, 139)


class PdfIndisponivel(Exception):
    pass


def _logo_reduzida(texto_base64: str) -> str:
    """Logo do ERP (base64) conferida como imagem; grande demais é reduzida. Vazio se não for uma imagem válida."""
    if not texto_base64 or len(texto_base64) > LOGO_MAX_BYTES * 4 // 3:
        return ""
    try:
        bruto = base64.b64decode(texto_base64.split(",")[-1], validate=False)
        with Image.open(io.BytesIO(bruto)) as img:
            if img.width * img.height > 25_000_000:
                return ""
            img.load()
            if img.format in ("PNG", "JPEG") and max(img.size) <= LOGO_MAX_PX * 2 and len(bruto) <= 300_000:
                return base64.b64encode(bruto).decode()   # já é pequena: vai como veio
            menor = img.convert("RGBA")
        menor.thumbnail((LOGO_MAX_PX, LOGO_MAX_PX))
        saida = io.BytesIO()
        menor.save(saida, format="PNG", optimize=True)
        return base64.b64encode(saida.getvalue()).decode()
    except Exception:
        logger.warning("logo da empresa inválida; PDF sai sem logo")
        return ""


async def _ler_empresa(token: str) -> dict:
    e = await MeuERPClient(token).get("/api/empresa/v1")
    return {
        "fantasia": (e.get("fantasia") or "").strip(), "razao": (e.get("razaoSocial") or "").strip(),
        "cnpj": e.get("cnpjCpf") or "", "logradouro": (e.get("logradouro") or "").strip(), "numero": (e.get("numero") or "").strip(),
        "bairro": (e.get("bairro") or "").strip(), "municipio": (e.get("municipio") or "").strip(), "uf": (e.get("uf") or "").strip(),
        "cep": e.get("cep") or "", "fone": (e.get("fone") or "").strip(),
        "logo": await asyncio.to_thread(_logo_reduzida, e.get("logo") or ""),
    }


async def dados_da_empresa(tenant: dict) -> dict:
    return await guardado(tenant["cnpj"], "empresa-pdf", TTL_EMPRESA, lambda: _ler_empresa(tenant["api_token"]))


# ---- Formatação -----------------------------------------------------------------------------------------------------
def _t(texto) -> str:
    """As fontes padrão do PDF só têm Latin-1: troca o que não couber."""
    s = str(texto or "").replace("−", "-").replace("–", "-").replace("—", "-").replace("“", '"').replace("”", '"').replace("’", "'")
    return s.encode("latin-1", "replace").decode("latin-1")


def _moeda(v: float) -> str:
    return "R$ " + f"{float(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _qtd(v: float) -> str:
    v = float(v or 0)
    return str(int(v)) if v == int(v) else f"{v:,.3f}".rstrip("0").replace(",", "X").replace(".", ",").replace("X", ".")


def _doc(d: str) -> str:
    d = "".join(c for c in str(d or "") if c.isdigit())
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def _fone(f: str) -> str:
    d = "".join(c for c in str(f or "") if c.isdigit())
    if len(d) == 11:
        return f"({d[:2]}) {d[2]} {d[3:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return str(f or "")


def _data(texto: str) -> str:
    t = str(texto or "")
    return f"{t[8:10]}/{t[5:7]}/{t[:4]} {t[11:16]}".strip() if len(t) >= 16 else t


# ---- Montagem -------------------------------------------------------------------------------------------------------
def _montar(empresa: dict, pedido: dict, cliente: dict, produtos: dict, formas: dict, vendedor: str) -> bytes:
    dados, documento = pedido["dados"], pedido["documento"]
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(12, 12, 12)
    pdf.set_title(_t(f"Pedido de venda {documento}"))
    pdf.set_creator("Prime Vendas")
    pdf.add_page()
    largura = pdf.w - 24

    # Cabeçalho: logo, loja e número do pedido
    x_texto = 12
    if empresa.get("logo"):
        try:
            img = io.BytesIO(base64.b64decode(empresa["logo"]))
            pdf.image(img, x=12, y=12, w=0, h=26, keep_aspect_ratio=True)
            with Image.open(io.BytesIO(base64.b64decode(empresa["logo"]))) as i:
                x_texto = 12 + min(26 * i.width / i.height, 50) + 4
        except Exception:
            logger.warning("não foi possível desenhar a logo no PDF")
    caixa_num = 52
    pdf.set_xy(x_texto, 12)
    largura_loja = pdf.w - 12 - caixa_num - x_texto - 2
    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(largura_loja, 6, _t(empresa.get("fantasia") or empresa.get("razao")), new_x="LEFT", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*CINZA)
    if empresa.get("razao") and empresa.get("razao") != empresa.get("fantasia"):
        pdf.multi_cell(largura_loja, 4.2, _t(empresa["razao"]), new_x="LEFT", new_y="NEXT")
    if empresa.get("cnpj"):
        pdf.multi_cell(largura_loja, 4.2, _t("CNPJ " + _doc(empresa["cnpj"])), new_x="LEFT", new_y="NEXT")
    rua = ", ".join(x for x in (empresa.get("logradouro"), empresa.get("numero")) if x)
    cidade = "/".join(x for x in (empresa.get("municipio"), empresa.get("uf")) if x)
    endereco = " - ".join(x for x in (rua, empresa.get("bairro"), cidade) if x)
    if endereco:
        pdf.multi_cell(largura_loja, 4.2, _t(endereco), new_x="LEFT", new_y="NEXT")
    if empresa.get("fone"):
        pdf.multi_cell(largura_loja, 4.2, _t("Fone " + _fone(empresa["fone"])), new_x="LEFT", new_y="NEXT")
    fim_loja = pdf.get_y()

    pdf.set_text_color(*AZUL)
    pdf.set_draw_color(*AZUL)
    pdf.rect(pdf.w - 12 - caixa_num, 12, caixa_num, 26)
    pdf.set_xy(pdf.w - 12 - caixa_num, 14)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(caixa_num, 5, "PEDIDO DE VENDA", align="C", new_x="LEFT", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(caixa_num, 9, _t(f"Nº {documento}"), align="C", new_x="LEFT", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*CINZA)
    pdf.cell(caixa_num, 5, _t(_data(dados.get("criado_em"))), align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.set_draw_color(203, 213, 225)

    pdf.set_y(max(fim_loja, 38) + 4)
    pdf.line(12, pdf.get_y(), pdf.w - 12, pdf.get_y())
    pdf.ln(3)

    # Cliente e vendedor
    pdf.set_font("Helvetica", "B", 10)
    pdf.multi_cell(largura, 5.5, _t("Cliente: " + (cliente.get("nome") or dados.get("cliente_nome") or "")), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    linha = [x for x in (
        ("CPF/CNPJ " + _doc(cliente["documento"])) if cliente.get("documento") else "",
        ("Fone " + _fone(cliente["fone"])) if cliente.get("fone") else "",
        "/".join(x for x in (cliente.get("cidade"), cliente.get("uf")) if x),
    ) if x]
    if linha:
        pdf.multi_cell(largura, 5, _t("   ".join(linha)), new_x="LMARGIN", new_y="NEXT")
    if vendedor:
        pdf.multi_cell(largura, 5, _t("Vendedor: " + vendedor), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Itens (coluna de acréscimo só se algum item tiver)
    bruto = descontos = acrescimos = 0.0
    com_acrescimo = any(float(i.get("acrescimo") or 0) for i in dados.get("itens") or [])
    larguras = (14, 66, 15, 22, 21, 21, 27) if com_acrescimo else (14, 78, 16, 24, 22, 28)
    titulos = ("Cód.", "Produto", "Qtd", "Preço", "Desconto", "Acréscimo", "Total") if com_acrescimo else ("Cód.", "Produto", "Qtd", "Preço", "Desconto", "Total")
    pdf.set_font("Helvetica", "", 8.5)
    with pdf.table(col_widths=larguras, text_align=("LEFT", "LEFT") + ("RIGHT",) * (len(larguras) - 2),
                   headings_style=FontFace(emphasis="BOLD", fill_color=(239, 243, 250)), line_height=5.2,
                   borders_layout="HORIZONTAL_LINES", padding=1.2) as tabela:
        cab = tabela.row()
        for titulo in titulos:
            cab.cell(_t(titulo))
        for item in dados.get("itens") or []:
            qtd, preco, desc = float(item.get("qtd") or 0), float(item.get("preco") or 0), float(item.get("desconto") or 0)
            acr = float(item.get("acrescimo") or 0)
            total_item = round(qtd * preco, 2)
            bruto += total_item
            descontos += desc
            acrescimos += acr
            prod = produtos.get(int(item.get("produto") or 0)) or {}
            descricao = prod.get("descricao") or item.get("descricao") or ""
            if prod.get("embalagem"):
                descricao += f" ({prod['embalagem']})"
            r = tabela.row()
            r.cell(_t(item.get("produto")))
            r.cell(_t(descricao))
            r.cell(_t(_qtd(qtd)))
            r.cell(_t(_moeda(preco)))
            r.cell(_t(_moeda(desc) if desc else "-"))
            if com_acrescimo:
                r.cell(_t(_moeda(acr) if acr else "-"))
            r.cell(_t(_moeda(total_item - desc + acr)))
    pdf.ln(2)

    # Totais
    def total(rotulo: str, valor: str, negrito: bool = False, tamanho: float = 9.5) -> None:
        pdf.set_font("Helvetica", "B" if negrito else "", tamanho)
        pdf.cell(largura - 40, 6, _t(rotulo), align="R")
        pdf.cell(40, 6, _t(valor), align="R", new_x="LMARGIN", new_y="NEXT")
    total("Produtos", _moeda(bruto))
    if descontos:
        total("Descontos", "- " + _moeda(descontos))
    if acrescimos:
        total("Acréscimos", "+ " + _moeda(acrescimos))
    total("Total do pedido", _moeda(pedido["total"]), negrito=True, tamanho=12)
    pdf.ln(3)

    # Pagamento
    pagamentos = dados.get("pagamentos") or []
    if pagamentos:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(largura, 6, "Pagamento", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for pg in pagamentos:
            forma = formas.get(int(pg.get("forma") or 0)) or {}
            nome = forma.get("descricao") or pg.get("forma_nome") or "Pagamento"
            plano = next((p for p in forma.get("parcelamentos") or [] if p["id"] == int(pg.get("parcelamento") or 0)), None)
            detalhe = f"{plano['descricao']} - {plano['parcelas']}x" if plano else ""
            pdf.cell(largura - 40, 5.5, _t(nome + (f"  ({detalhe})" if detalhe else "")))
            pdf.cell(40, 5.5, _t(_moeda(pg.get("valor"))), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    if dados.get("observacao"):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(largura, 6, _t("Observação"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(largura, 5, _t(str(dados["observacao"])[:500]), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*CINZA)
    pdf.multi_cell(largura, 4.2, _t("Documento sem valor fiscal. Preços e condições sujeitos à confirmação da loja.\nGerado pelo Prime Vendas."),
                   align="C", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


async def gerar(aparelho: dict, id_pedido: str) -> tuple[bytes, str]:
    """PDF de um pedido do próprio vendedor que já entrou no ERP. Devolve (bytes, nome do arquivo)."""
    tenant, cnpj = aparelho["tenant"], aparelho["cnpj"]
    linha = pedidos._linha(str(id_pedido).lower())
    if linha is None or linha["cnpj"] != cnpj or int(linha["id_funcionario"]) != int(aparelho["id_funcionario"]):
        raise pedidos.PedidoRecusado("Pedido não encontrado.")
    if not linha["id_documento"] or linha["situacao"] in ("recusado", "cancelado"):
        raise pedidos.PedidoRecusado("O PDF fica disponível para pedidos aceitos pelo ERP e não cancelados.")
    pedido = pedidos._para_celular(linha)
    pedido["total"] = pedido["resumo"].get("total", pedido["dados"].get("total") or 0)

    try:
        empresa = await dados_da_empresa(tenant)
    except Exception:
        logger.warning("dados da empresa %s indisponíveis para o PDF; sai só com o nome", cnpj)
        empresa = {"fantasia": tenant.get("nome_fantasia") or "", "cnpj": cnpj}
    try:
        formas = {f["id"]: f for f in (await pedidos.config_da_empresa(tenant))[linha["tipo"]]["pagamentos"]}
    except Exception:
        formas = {}

    ids = [int(i.get("produto") or 0) for i in pedido["dados"].get("itens") or []][:300]
    id_cliente = int(pedido["resumo"].get("cliente") or pedido["dados"].get("cliente") or 0)
    with db_session() as conn:
        produtos = {int(r["id"]): dict(r) for r in conn.execute(
            f"SELECT id, descricao, embalagem FROM pv_produtos WHERE cnpj = ? AND id IN ({', '.join('?' * len(ids)) or 'NULL'})",
            (cnpj, *ids))} if ids else {}
        r = conn.execute("SELECT nome, documento, fone, cidade, uf FROM pv_clientes WHERE cnpj = ? AND id = ?", (cnpj, id_cliente)).fetchone()
    cliente = dict(r) if r else {"nome": pedido["resumo"].get("cliente_nome") or ""}

    conteudo = await asyncio.to_thread(_montar, empresa, pedido, cliente, produtos, formas, aparelho.get("nome_vendedor") or "")
    return conteudo, f"Pedido-{linha['id_documento']}.pdf"
