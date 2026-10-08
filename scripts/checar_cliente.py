"""Conferência de um cliente novo: roda os mesmos cálculos do app e mostra o que comparar com o ERP.

Uso (na pasta do projeto, com o ambiente virtual):
    python scripts/checar_cliente.py CNPJ [AAAA-MM-DD]

O cliente precisa estar no banco local (primesys.db). O dia padrão é ontem. Só LÊ do ERP (cerca de 8
chamadas) e não imprime token nem senha. Leia o resultado ao lado das telas do ERP do cliente (ver
docs/roteiro-cliente-novo.md).
"""

import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app import estoque, financeiro, tenants, vendas  # noqa: E402
from app.meuerp_client import MeuERPClient  # noqa: E402
from app.sqlquery import executar  # noqa: E402

AVISOS: list[str] = []


def reais(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def secao(titulo: str) -> None:
    print(f"\n== {titulo}")


async def main(cnpj: str, dia: date) -> None:
    tenant = tenants.get_tenant_by_cnpj(cnpj)
    if tenant is None:
        sys.exit("Cliente não encontrado (ou inativo) no banco local. Cadastre-o primeiro.")
    client = MeuERPClient(tenant["api_token"])
    print(f"Cliente: {tenant['nome_fantasia']} (CNPJ {tenant['cnpj']}) · dia conferido: {dia:%d/%m/%Y}")

    # 1) Como o cliente vende
    secao("Como vende (últimos 30 dias)")
    linhas = await executar(
        client,
        f"""select d.modelo, d.status, count(*) as n from documento d
            where d.tipomovimento = 'S' and d.datahora >= {vendas.d(date.today() - timedelta(days=30))}
              and d.modelo in ('55','65','PV') group by 1, 2 order by 1, 2""",
    )
    nomes = {"65": "NFC-e", "55": "NF-e", "PV": "Pré-venda"}
    emitidas = {}
    for l in linhas:
        print(f"  {nomes[l['modelo']]:<10} status {l['status']}: {int(l['n'])}")
        if l["status"] == "E":
            emitidas[l["modelo"]] = int(l["n"])
    if len(emitidas) > 1:
        print("  -> vende em mais de um tipo: o bloco 'Vendas por tipo' vai aparecer no dashboard.")
    if not emitidas:
        AVISOS.append("Nenhuma venda emitida nos últimos 30 dias: confira se o token é da empresa certa.")

    # 2) Vendas do dia (comparar com o ERP)
    secao(f"Vendas de {dia:%d/%m/%Y} (compare com o relatório de vendas do ERP)")
    resumo = await vendas.resumo(client, dia, dia)
    formas, por_tipo = await vendas.pagamentos(client, dia, dia)
    print(f"  Total: {reais(resumo['total'])} em {resumo['vendas']} vendas · ticket médio {reais(resumo['ticket_medio'])}")
    for t in por_tipo:
        print(f"    {t['tipo']}: {reais(t['total'])} ({t['vendas']} vendas)")
    print("  Formas de pagamento:")
    for f in formas:
        print(f"    {f['forma']:<22} {reais(f['valor']):>16}  {f['percentual']:>5.1f}%")
    sem_forma = round(resumo["total"] - sum(f["valor"] for f in formas), 2)
    if abs(sem_forma) > 1:
        AVISOS.append(f"{reais(sem_forma)} do total do dia sem forma de pagamento lançada.")
    if resumo["vendas"] == 0:
        AVISOS.append("Sem vendas no dia escolhido: tente outra data para comparar com o ERP.")

    # 3) Estoque
    secao("Estoque (compare com o relatório de estoque do ERP)")
    e = await estoque.posicao(client)
    print(f"  A custo: {reais(e['valorCusto'])} · a preço de venda: {reais(e['valorVenda'])}")
    print(f"  Mercadorias ativas: {e['ativas']} · com estoque: {e['comEstoque']} · negativas: {e['negativos']}")
    if e["comEstoque"] and e["semCusto"] / e["comEstoque"] > 0.05:
        AVISOS.append(f"{e['semCusto']} produtos com estoque e sem custo cadastrado ({e['semCusto'] / e['comEstoque']:.0%}): o valor a custo fica subestimado.")
    if e["semPreco"]:
        AVISOS.append(f"{e['semPreco']} produtos com estoque e sem preço de venda.")
    if e["ativas"] and e["negativos"] / e["ativas"] > 0.02:
        AVISOS.append(f"{e['negativos']} produtos com estoque negativo ({e['negativos'] / e['ativas']:.0%} dos ativos): entradas lançadas com atraso?")

    # 4) Financeiro
    secao("Financeiro (compare com contas a pagar e a receber do ERP)")
    fin = await financeiro.posicao(client, date.today())
    for chave, rotulo in (("pagar", "A pagar"), ("receber", "A receber")):
        bloco = fin[chave]
        print(f"  {rotulo}: {reais(bloco['valor'])} em {bloco['quantidade']} títulos")
        atrasadas = next((f for f in bloco["faixas"] if f["id"] == "atrasadas"), None)
        if atrasadas and atrasadas["quantidade"]:
            print(f"    atrasadas: {reais(atrasadas['valor'])} ({atrasadas['quantidade']} títulos)")
            if bloco["valor"] and atrasadas["valor"] / bloco["valor"] > 0.5:
                AVISOS.append(f"{rotulo}: {atrasadas['valor'] / bloco['valor']:.0%} do valor está atrasado; pode haver títulos pagos sem baixa no ERP.")

    secao("Pontos de atenção")
    print("\n".join(f"  - {a}" for a in AVISOS) if AVISOS else "  Nenhum.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    escolhido = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today() - timedelta(days=1)
    asyncio.run(main(sys.argv[1], escolhido))
