"""Fechamento de caixa: o que o sistema esperava, o que o operador contou e a diferença por forma de pagamento.

No ERP, cada fechamento é um documento do modelo FC. Para cada forma de pagamento ele guarda `valdisponivel`
(esperado: vendas + entradas - retiradas) e `valconferido` (contado pelo operador). Validado contra a tela
"Conferência de Caixa" do ERP (HIPER, 05/10/2026): esperado, contado e diferença batem em todas as formas.

Regras:
  - fechamento com esperado e contado iguais a zero é ignorado (caixa aberto sem venda);
  - fechamento em que nada foi contado fica como "sem conferência" e não entra nas somas (antes de o cliente
    começar a conferir, o contado é zero e isso NÃO é falta);
  - fechamento em que só o dinheiro ficou sem contagem fica como "dinheiro não contado", também fora das somas;
  - diferença = contado - esperado (positivo = sobra, negativo = falta).
"""

import json
import re
from datetime import date, datetime, timedelta

from app.meuerp_client import MeuERPClient
from app.sqlquery import d, executar

MAX_DIAS = 92
MAX_FECHAMENTOS = 60
# A partir deste valor (R$) a diferença do dinheiro entra no resumo de faltas e sobras.
LIMITE_RESUMO = 5.0


def _num(valor) -> float:
    return float(valor or 0)


def _primeiro_nome(bruto: str | None) -> str:
    for parte in re.split(r"[\s@._\-]+", (bruto or "").strip()):
        letras = re.sub(r"[^A-Za-zÀ-ÿ]", "", parte)
        if letras:
            return letras.capitalize()
    return "Não informado"


def _eh_dinheiro(forma: str) -> bool:
    return (forma or "").strip().upper() == "DINHEIRO"


async def fechamentos(client: MeuERPClient, inicio: date, fim: date) -> dict:
    linhas = await executar(
        client,
        f"""select d._iddocumento as id, d.numero, to_char(d.datahora, 'YYYY-MM-DD HH24:MI:SS') as quando,
                   d.idabertura as abertura, d.idcaixa as caixa, d.nomeusuario as operador,
                   to_char((select max(a.datahora) from documento a
                             where a.modelo = 'AX' and a.idcaixa = d.idcaixa and a.idabertura = d.idabertura
                               and a.idusuario = d.idusuarioabertura and a.datahora <= d.datahora
                               and a.datahora >= {d(inicio - timedelta(days=3))}),
                           'YYYY-MM-DD HH24:MI:SS') as aberto_em,
                   (select max(x.nomeusuario) from documento x
                     where x.idusuario = dc.idusuario and x.datahora >= {d(inicio - timedelta(days=90))}) as conferente,
                   (dc._iddocumento is not null) as conferido_por_outro,
                   json_agg(json_build_object('forma', c.descricao, 'esperado', c.valdisponivel, 'contado', c.valconferido)
                            order by c.valdisponivel desc, c.descricao) as formas
            from documento d
            join documento_conferencia_caixa c on c._iddocumento = d._iddocumento
            left join documento_conferencia dc on dc._iddocumento = d._iddocumento
            where d.modelo = 'FC' and d.status = 'E'
              and d.datahora >= {d(inicio)} and d.datahora < {d(fim + timedelta(days=1))}
            group by d._iddocumento, d.numero, d.datahora, d.idabertura, d.idcaixa, d.idusuarioabertura, d.nomeusuario,
                     dc._iddocumento, dc.idusuario
            having sum(c.valdisponivel) <> 0 or sum(c.valconferido) <> 0
            order by d.datahora desc limit {MAX_FECHAMENTOS}""",
    )

    itens = []
    for l in linhas:
        bruto = l["formas"]
        formas_brutas = json.loads(bruto) if isinstance(bruto, str) else (bruto or [])
        formas = [
            {
                "forma": f["forma"],
                "esperado": _num(f["esperado"]),
                "contado": _num(f["contado"]),
                "diferenca": round(_num(f["contado"]) - _num(f["esperado"]), 2),
            }
            for f in formas_brutas
            if _num(f["esperado"]) or _num(f["contado"])  # forma sem movimento não precisa aparecer
        ]
        dinheiro = next((f for f in formas if _eh_dinheiro(f["forma"])), None)
        total_esperado = round(sum(f["esperado"] for f in formas), 2)
        total_contado = round(sum(f["contado"] for f in formas), 2)

        if total_contado == 0:
            situacao = "sem_conferencia"
        elif dinheiro and dinheiro["esperado"] > 0 and dinheiro["contado"] == 0:
            situacao = "dinheiro_nao_contado"
        else:
            situacao = "conferido"

        quando = datetime.strptime(l["quando"], "%Y-%m-%d %H:%M:%S")
        aberto = datetime.strptime(l["aberto_em"], "%Y-%m-%d %H:%M:%S") if l["aberto_em"] else None
        operador = _primeiro_nome(l["operador"])
        outras = [f for f in formas if not _eh_dinheiro(f["forma"])]
        dif_dinheiro = dinheiro["diferenca"] if dinheiro else 0.0
        dif_outras = round(sum(f["diferenca"] for f in outras), 2)
        dif_total = round(total_contado - total_esperado, 2)
        # Falta (ou sobra) no dinheiro quase igual à sobra (ou falta) nas outras formas: troca de forma de pagamento.
        compensada = (
            situacao == "conferido"
            and abs(dif_dinheiro) >= LIMITE_RESUMO
            and dif_dinheiro * dif_outras < 0
            and abs(dif_total) <= 0.1 * abs(dif_dinheiro)
        )
        itens.append(
            {
                "id": l["id"],
                "numero": l["numero"],
                "data": quando.strftime("%d/%m"),
                "hora": quando.strftime("%H:%M"),
                "abertoEm": (
                    {"data": aberto.strftime("%d/%m"), "hora": aberto.strftime("%H:%M")} if aberto else None
                ),
                "abertura": l["abertura"],
                "caixa": l["caixa"],
                "operador": operador,
                "conferente": _primeiro_nome(l["conferente"]) if l["conferido_por_outro"] and l["conferente"] else operador,
                "situacao": situacao,
                "formas": formas,
                "dinheiro": dinheiro,
                "totalEsperado": total_esperado,
                "totalContado": total_contado,
                "difTotal": dif_total,
                "difOutras": dif_outras,
                "compensada": compensada,
            }
        )

    conferidos = [i for i in itens if i["situacao"] == "conferido" and i["dinheiro"]]
    relevantes = [i["dinheiro"]["diferenca"] for i in conferidos if abs(i["dinheiro"]["diferenca"]) >= LIMITE_RESUMO]
    return {
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "fechamentos": itens,
        "resumo": {
            "conferidos": len(conferidos),
            "faltas": round(sum(v for v in relevantes if v < 0), 2),
            "qtdFaltas": sum(1 for v in relevantes if v < 0),
            "sobras": round(sum(v for v in relevantes if v > 0), 2),
            "qtdSobras": sum(1 for v in relevantes if v > 0),
            "semConferencia": sum(1 for i in itens if i["situacao"] == "sem_conferencia"),
            "dinheiroNaoContado": sum(1 for i in itens if i["situacao"] == "dinheiro_nao_contado"),
            "limite": LIMITE_RESUMO,
        },
        "truncado": len(linhas) >= MAX_FECHAMENTOS,
    }
