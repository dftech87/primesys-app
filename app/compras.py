"""Entradas (compras) de um produto: as últimas notas fiscais em que ele chegou na loja."""

import re
from datetime import date, datetime, timedelta

from app.meuerp_client import MeuERPClient
from app.sqlquery import d, executar, i

# NF-e de entrada (55) e nota modelo 1A. Os modelos AS/AC do ERP são acertos internos de estoque, não compras.
_MODELOS_ENTRADA = "('55', '1A')"
MAX_ENTRADAS = 30


def _num(valor) -> float:
    return float(valor or 0)


async def entradas_produto(client: MeuERPClient, id_variacao: int, limite: int = 10) -> list[dict]:
    """Últimas notas de entrada do produto, da mais nova para a mais antiga (uma consulta).

    `custoUnit` é o custo por unidade de estoque (já dividido pelo fator da embalagem); `precoVenda` é o preço de venda que o ERP gravou na
    entrada (pode vir 0 quando o ERP não gravou: a tela usa o preço atual nesse caso).
    """
    limite = max(1, min(int(limite), MAX_ENTRADAS))
    linhas = await executar(
        client,
        f"""select to_char(coalesce(d.datahoramovimento, d.datahora), 'DD/MM/YYYY') as data,
                   d.numero, d.serie, trim(coalesce(p.nome, '')) as fornecedor,
                   dm.qtd, dm.embalagem, dm.qtdfator as fator, dm.qtdestoque as qtd_estoque,
                   dm.valunitarioliquido as valor_nf, dm.valdesconto as desconto,
                   c.valcusto as custo_unit, c.valcustomedio as custo_medio,
                   (select pr.valpreco from documento_mercadoria_preco pr
                     where pr._iddocumento = dm._iddocumento and pr._idsequencia = dm._idsequencia
                     order by pr._idtabela limit 1) as preco_venda
            from documento d
            join documento_mercadoria dm on dm._iddocumento = d._iddocumento
            left join documento_mercadoria_custo c
              on c._iddocumento = dm._iddocumento and c._idsequencia = dm._idsequencia
            left join pessoa p on p._idpessoa = d.idpessoa
            where d.tipomovimento = 'E' and d.status = 'E' and d.modelo in {_MODELOS_ENTRADA}
              and dm.idmercadoriavariacao = {i(id_variacao)}
            order by coalesce(d.datahoramovimento, d.datahora) desc, d.numero desc
            limit {i(limite)}""",
    )
    resultado = []
    for l in linhas:
        fator = _num(l["fator"]) or 1.0
        valor_nf = _num(l["valor_nf"])
        # Custo por UNIDADE de estoque: o ERP grava em valcusto (a nota vem com o preço da embalagem,
        # ex.: caixa com 8 a R$ 8,64 = R$ 1,08 a unidade). Sem custo gravado, divide o valor da nota pelo fator.
        custo_unit = _num(l["custo_unit"]) or valor_nf / fator
        resultado.append(
            {
                "data": l["data"],
                "numero": l["numero"],
                "serie": l["serie"],
                "fornecedor": l["fornecedor"] or "Fornecedor não informado",
                "qtd": _num(l["qtd"]),
                "unidade": l["embalagem"],
                "fator": fator,
                "qtdUnidades": _num(l["qtd_estoque"]) or _num(l["qtd"]) * fator,
                "valorNF": valor_nf,
                "custoUnit": round(custo_unit, 4),
                "desconto": _num(l["desconto"]),
                "custoMedio": _num(l["custo_medio"]),
                "precoVenda": _num(l["preco_venda"]),
            }
        )
    return resultado


# ---------------------------------------------------------------------------------------------
# Fornecedores do produto (comparação de custo) e histórico do preço de venda
# ---------------------------------------------------------------------------------------------

MESES_FORNECEDORES = 12
# Mudança de preço que passa deste percentual e volta ao valor anterior em poucos dias: provável erro.
_LIMITE_ERRO_PRECO = 0.30
_DIAS_PARA_VOLTAR = 15


async def fornecedores_produto(client: MeuERPClient, id_variacao: int, hoje: date) -> dict:
    """Custo por fornecedor nas entradas dos últimos 12 meses, do mais barato (último custo) ao mais caro."""
    desde = hoje - timedelta(days=30 * MESES_FORNECEDORES)
    linhas = await executar(
        client,
        f"""with e as (
              select d.idpessoa, trim(coalesce(p.nome, '')) as fornecedor,
                     coalesce(d.datahoramovimento, d.datahora) as dt,
                     case when c.valcusto > 0 then c.valcusto
                          else dm.valunitarioliquido / nullif(dm.qtdfator, 0) end as custo
              from documento d
              join documento_mercadoria dm on dm._iddocumento = d._iddocumento
              left join documento_mercadoria_custo c
                on c._iddocumento = dm._iddocumento and c._idsequencia = dm._idsequencia
              left join pessoa p on p._idpessoa = d.idpessoa
              where d.tipomovimento = 'E' and d.status = 'E' and d.modelo in {_MODELOS_ENTRADA}
                and dm.idmercadoriavariacao = {i(id_variacao)}
                and coalesce(d.datahoramovimento, d.datahora) >= {d(desde)}
            )
            select max(fornecedor) as fornecedor, count(*) as entradas,
                   round(min(custo), 4) as menor, round(max(custo), 4) as maior,
                   round((array_agg(custo order by dt desc))[1], 4) as ultimo,
                   to_char(max(dt), 'DD/MM/YYYY') as ultima, to_char(max(dt), 'YYYY-MM-DD') as ultima_iso
            from e where custo > 0 group by idpessoa order by 5 asc limit 8""",
    )
    fornecedores = [
        {
            "fornecedor": l["fornecedor"] or "Fornecedor não informado",
            "entradas": int(l["entradas"]),
            "menor": _num(l["menor"]),
            "maior": _num(l["maior"]),
            "ultimoCusto": _num(l["ultimo"]),
            "ultimaCompra": l["ultima"],
            "_iso": l["ultima_iso"],
        }
        for l in linhas
    ]
    ultimo = max(fornecedores, key=lambda f: f["_iso"])["fornecedor"] if fornecedores else None
    for f in fornecedores:
        f.pop("_iso")
    return {"meses": MESES_FORNECEDORES, "fornecedores": fornecedores, "fornecedorUltimaCompra": ultimo}


def nome_publico(bruto: str | None) -> str:
    """Só o primeiro nome de quem alterou: o e-mail do funcionário nunca sai do servidor."""
    texto = (bruto or "").strip().split("@", 1)[0]
    for parte in re.split(r"[._\-\s]+", texto):
        letras = re.sub(r"[^A-Za-zÀ-ÿ]", "", parte)
        if letras:
            return letras.capitalize()
    return "Não informado"


async def historico_preco(client: MeuERPClient, id_variacao: int, limite: int = 30) -> list[dict]:
    """Mudanças do preço de venda (tabela principal do produto), da mais nova para a mais antiga.

    Marca `possivelErro` quando o preço mudou mais de 30% e voltou ao valor anterior em até 15 dias
    (ex.: 1,46 -> 7,16 -> 1,46), e `corrigiu` na mudança que desfez o erro.
    """
    linhas = await executar(
        client,
        f"""select to_char(h.datahora, 'YYYY-MM-DD HH24:MI:SS') as quando, to_char(h.datahora, 'DD/MM/YYYY') as data,
                   h.valprecotual as de, h.valpreconovo as para, h.nomeusuarioalteracao as quem
            from mercadoria_tabela_preco_historico h
            where h._idmercadoriavariacao = {i(id_variacao)}
              and h._idtabela = (select min(x._idtabela) from mercadoria_tabela_preco_historico x
                                 where x._idmercadoriavariacao = {i(id_variacao)})
            order by h.datahora desc limit {i(max(1, min(int(limite), 60)))}""",
    )
    mudancas = [
        {
            "quando": datetime.strptime(l["quando"], "%Y-%m-%d %H:%M:%S"),
            "data": l["data"],
            "de": _num(l["de"]),
            "para": _num(l["para"]),
            "quem": nome_publico(l["quem"]),
            "possivelErro": False,
            "corrigiu": False,
            "voltouEmDias": None,
        }
        for l in linhas
    ]
    antigas_primeiro = sorted(mudancas, key=lambda m: m["quando"])
    for k, m in enumerate(antigas_primeiro):
        if m["de"] <= 0 or abs(m["para"] - m["de"]) / m["de"] < _LIMITE_ERRO_PRECO:
            continue
        for depois in antigas_primeiro[k + 1:]:
            dias = (depois["quando"] - m["quando"]).days
            if dias > _DIAS_PARA_VOLTAR:
                break
            if abs(depois["para"] - m["de"]) / m["de"] <= 0.05:
                m["possivelErro"], m["voltouEmDias"], depois["corrigiu"] = True, dias, True
                break
    for m in mudancas:
        m["variacaoPct"] = round((m["para"] - m["de"]) / m["de"] * 100, 1) if m["de"] > 0 else None
        m["quando"] = m["quando"].isoformat()
    return mudancas
