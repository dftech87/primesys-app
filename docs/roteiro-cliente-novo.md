# Roteiro para entrada de cliente novo

Objetivo: antes do cliente usar o app, conferir que os números batem com o ERP dele e saber de antemão o que
vai parecer estranho (dado sujo no cadastro), para ele não estranhar primeiro.

## 1. Cadastro
1. No ERP do cliente, gerar o **token da API pública** (ele não passa por chat, e-mail ou mensagem: só vai
   direto para o campo do `/admin`).
2. Em `/admin` de produção: cadastrar CNPJ, nome fantasia e token. O token fica criptografado no banco.
3. Para rodar a conferência abaixo, cadastrar o mesmo cliente no banco **local** (o `/admin` do servidor local
   ou `scripts/seed_tenant.py`). O banco local não vai para o GitHub.
4. Os usuários entram com **CNPJ + e-mail e senha do próprio ERP** (o app valida direto no ERP e não guarda
   senha). Login só por e-mail, não por nome de usuário.

## 2. Conferência automática
```
python scripts/checar_cliente.py CNPJ AAAA-MM-DD
```
Use um dia **completo** de vendas. Ele mostra como o cliente vende (NFC-e, pré-venda, NF-e), as vendas do dia
por tipo e forma de pagamento, o estoque, o financeiro e uma lista de **pontos de atenção**. Só lê (cerca de
8 chamadas ao ERP) e não imprime token.

## 3. Comparar com o ERP (o que o script não faz sozinho)
| No app (saída do script) | Onde conferir no ERP | Tem que bater |
|---|---|---|
| Total e quantidade de vendas do dia | Relatório de vendas do dia | Centavo a centavo |
| Total por tipo (NFC-e / pré-venda) | Lista de documentos filtrada por modelo | Centavo a centavo |
| Formas de pagamento (troco já abatido do dinheiro) | Fechamento de caixa do dia | Centavo a centavo |
| Estoque a custo e a venda, produtos ativos | Relatório de estoque | Pequena diferença só se houver produto sem custo |
| A pagar / a receber e atrasadas | Telas de contas a pagar e a receber | Mesmo total e mesma quantidade |

Se algo não bater, **não publique o cliente**: investigue primeiro (veja "Armadilhas conhecidas").

## 4. Conferências manuais rápidas
- **Fechamento de caixa:** abrir um fechamento no app (aba Conferência) e comparar com a tela de conferência
  de caixa do ERP (esperado, contado, diferença, quem conferiu).
- **Margem:** o app calcula com o custo gravado no momento da venda; deve ficar perto da margem do DRE do
  cliente (validado: diferença de cerca de 0,5 ponto).
- **Entradas:** pesquisar um produto na aba Compras e comparar as últimas notas com a tela de entradas do ERP.

## 5. Armadilhas conhecidas (já vistas em clientes reais)
- **Pré-venda e NFC-e são vendas separadas**: não somam duas vezes. Numa pré-venda, o troco é lançado como
  pagamento negativo; o app o abate do dinheiro.
- **Parcelas "fantasma"**: notas de saída rejeitadas ou de teste (status R/X) deixam parcelas a pagar no banco
  que o ERP não mostra. O app só conta documentos emitidos (status E).
- **Estoque sem custo**: produtos com saldo e sem custo cadastrado ficam fora do valor a custo (o bloco de
  Estoque avisa a quantidade).
- **Estoque negativo**: costuma ser entrada de nota lançada com atraso, não falta de mercadoria.
- **Contas atrasadas muito antigas**: costumam ser títulos já pagos e sem baixa no ERP.
- **Inativo**: o ERP grava `T`/`F` em `flaginativo` (alguns cadastros antigos usam `S`/`N`).
- **Nome do favorecido**: em alguns lançamentos o nome é "CONSUMIDOR FINAL" e o favorecido está na descrição.
- **Cota do ERP**: 20 chamadas por minuto por token. O app já usa cache; testes em paralelo com o uso real
  dividem essa cota.

## 6. Entrega ao cliente
1. Passar CNPJ e orientar o login com o **e-mail do ERP** (não o nome de usuário).
2. Avisar dos pontos de atenção encontrados no passo 2 (por exemplo, "o estoque negativo vem de entradas
   atrasadas").
3. Acompanhar o primeiro dia de uso: se aparecer diferença, comparar com o ERP antes de dizer que é erro do app.
