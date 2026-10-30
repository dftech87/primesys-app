# Permissões do perfil de usuário no Meu ERP (Prime Info e Prime Vendas)

Os dois apps seguem o **perfil do usuário no Meu ERP**. Para liberar ou esconder alguma coisa, ajuste o perfil
no Meu ERP (cadastro de **Perfil de usuário** → permissões). Não há nada para configurar nos apps.

- Mudança no perfil vale em até **1 hora** no Prime Info (ou na hora, se o usuário sair e entrar de novo).
- No Prime Vendas, vale em até algumas horas (ou ao entrar de novo).
- O número entre parênteses é o código da permissão no Meu ERP, para achar mais rápido.

---

## Prime Info

### Regra geral

- Usuário **com perfil** na empresa vê só o que o perfil libera (tabelas abaixo).
- Usuário **sem perfil** na empresa (ex.: usuário de suporte/revenda) **vê tudo**. Para restringir alguém, dê a ele
  um perfil no Meu ERP.
- Se o perfil não liberar **nenhuma** das abas abaixo, o login é recusado ("Seu perfil no Meu ERP não libera nenhuma
  área do Prime Info").

### Abas

| Aba do app | Liberar no perfil (módulo → permissão) |
|---|---|
| **Dashboard** | DASHBOARD → Permissão para acessar o módulo (**3301**) |
| **Produtos** (consulta) | CADASTRO DE MERCADORIA → Permissão para acessar o módulo (**1**) |
| **Compras** (notas de entrada) | CENTRAL DE COMPRAS → acessar o módulo (**6401**) **ou** OUTRAS ENTRADAS → acessar o módulo (**6501**) |
| **Conferência** (de caixa) | CONFERÊNCIA DE CAIXA → Permissão para acessar o módulo (**4001**) |
| **Financeiro** | CONTAS A PAGAR/RECEBER → acessar o módulo (**2401**) **e** pelo menos uma das duas abaixo |
| ↳ Contas **a pagar** | CONTAS A PAGAR/RECEBER → Permissão para visualizar contas a pagar (**2402**) |
| ↳ Contas **a receber** | CONTAS A PAGAR/RECEBER → Permissão para visualizar contas a receber (**2403**) |

### Dentro do Dashboard

| O que aparece | Liberar no perfil |
|---|---|
| Cartões **Margem** e **Lucro bruto** e o quadro **Margem de vendas** | DASHBOARD → visualizar dashboard 'Resumo - Lucro' (**3304**) |
| Quadro **Estoque** (valor do estoque) | DASHBOARD → visualizar seção de dashboard 'Estoque' (**3320**) |
| ↳ valor do estoque **a preço de custo** | também a permissão de custo (**9**, abaixo) |

### Ficha do produto (aba Produtos)

Descrição, códigos, unidade e **preço de venda** aparecem para todos que têm a aba Produtos.

| Informação da ficha | Liberar no perfil |
|---|---|
| **Preço de custo** e **margem** | CADASTRO DE MERCADORIA → Permissão para visualizar custos no cadastro de mercadoria (**9**) |
| **Estoque** na loja | POS → Permissão para consultar estoque (**10041**) **ou** MOVIMENTO DE ESTOQUE → acessar o módulo (**1901**) |
| **Histórico de preço** (quem alterou e quando) | FORMAÇÃO DE PREÇOS → acessar o módulo (**5201**) **ou** TABELAS DE PREÇO → acessar o módulo (**3501**) |
| Botão **Ver notas de entrada** | o mesmo da aba Compras (**6401** ou **6501**) |

### Exemplo: usuário que só consulta produtos e preço

Liberar apenas **CADASTRO DE MERCADORIA → acessar o módulo (1)**. Ele vê só a aba Produtos, com o preço de venda,
sem custo, estoque, histórico de preço e notas de entrada. Para ele ver o estoque também, liberar **10041**.

---

## Prime Vendas

### Para o vendedor conseguir entrar

- O Prime Vendas precisa estar **liberado** para a empresa no **/admin**.
- O funcionário precisa estar **ativo** no Meu ERP e ter o **usuário do ERP vinculado** no cadastro dele
  (ou o e-mail de login no contato do funcionário, mas aí sem as permissões abaixo).
- Detalhes em `liberar-prime-vendas.md`.

### O que o perfil libera no app

| Função no app | Liberar no perfil (módulo → permissão) |
|---|---|
| Dar **desconto no item** | POS → Permissão para aplicar desconto no Item (**10038**) |
| Dar **desconto no pedido** | POS → Permissão para aplicar desconto no Documento (**10037**) |
| Passar do **desconto máximo** do produto e vender **abaixo do ponto de equilíbrio** (só com aviso) | POS → Permissão para aplicar desconto maior que o configurado (**10042**) |
| Dar **acréscimo** | POS → Permissão para aplicar acréscimo no Documento (**10096**) |
| Ver o **valor do ponto de equilíbrio** (custo atual) e a **margem** na ficha do item | CADASTRO DE MERCADORIA → visualizar custos no cadastro de mercadoria (**9**) |

- Sem a permissão **9**, o vendedor vê só "🟢 acima" ou "🔴 abaixo do ponto de equilíbrio", sem o valor.
- Sem a permissão **10042**, o app **bloqueia** desconto que deixe o preço abaixo do custo (e também acima do desconto
  máximo do produto).
- O desconto máximo de cada produto é o do cadastro da mercadoria no Meu ERP.

---

*Atualizado em 30/10/2026. Se uma permissão mudar de nome no Meu ERP, procure pelo código entre parênteses.*
